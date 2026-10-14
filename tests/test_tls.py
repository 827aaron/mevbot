"""Real local HTTPS coverage of packaged model certificate verification."""
import json
import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request

from salesteam import db, llm, paths, settings, tls, vault
from salesteam.errors import UserError
from salesteam.llm import claude, decision, openai_compat, relay
from salesteam.llm.base import AgentConfig, LLMRequest
from tls_fixture import TLSFixture


class ModelTLSTests(unittest.TestCase):
    def setUp(self):
        db.close()
        vault.reset()
        data = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="jamesagent-tls-data-")))
        for name, value in {"DATA_DIR": data, "DB_PATH": data / "salesteam.db",
                            "KEY_PATH": data / ".secret.key", "UPLOAD_DIR": data / "uploads",
                            "EXPORT_DIR": data / "exports"}.items():
            self.enterContext(mock.patch.object(paths, name, value))
        # No proxy can route these local-only fixtures to an external service.
        self.enterContext(mock.patch.dict(os.environ, {"SSL_CERT_FILE": "", "SSL_CERT_DIR": "",
                            "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
                            "http_proxy": "", "https_proxy": "", "all_proxy": "",
                            "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}))
        self.enterContext(mock.patch.object(relay, "_catalog_cache", {}))
        self.addCleanup(db.close)
        self.addCleanup(vault.reset)
        db.connect()
        llm.init_defaults()
        self.api = self.enterContext(TLSFixture())

    def trust_fixture(self):
        self.enterContext(mock.patch.dict(os.environ, {"SSL_CERT_FILE": str(self.api.ca_file)}))

    def config(self, provider="openai_compat", base_url=None):
        return AgentConfig(agent="analyst", provider=provider, model=self.api.model,
                           api_key="qa-local-credential", base_url=base_url or self.api.base_url)

    def test_trusted_https_model_listing_saved_decision_and_choice_format(self):
        self.trust_fixture()
        payload = {"base_url": self.api.base_url, "api_key": "qa-local-credential", "protocol": "openai"}
        self.assertEqual(relay.list_remote(payload), [self.api.model])
        added = relay.add_relay(payload)
        configured = decision.configure(added["id"], self.api.model)
        self.assertTrue(configured["configured"])
        result = decision.test()
        self.assertEqual(result["choice"], "buyer")
        self.assertEqual(result["confidence"], .95)
        self.assertFalse(result["escalate"])
        self.assertEqual(decision.get_config()["relay"], added["id"])
        self.assertTrue(any(r["method"] == "GET" and r["path"] == "/v1/models" for r in self.api.requests))
        chats = [r for r in self.api.requests if r["path"] == "/v1/chat/completions"]
        self.assertTrue(chats)
        self.assertEqual(chats[-1]["body"]["model"], self.api.model)
        self.assertEqual(chats[-1]["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(db.query_one("SELECT COUNT(*) n FROM usage WHERE agent='decider'")["n"], 1)

    def test_choice_test_still_rejects_invalid_model_json(self):
        self.trust_fixture()
        added = relay.add_relay({"base_url": self.api.base_url, "api_key": "qa-local-credential",
                                 "protocol": "openai"})
        decision.configure(added["id"], self.api.model)
        self.api.malformed_decision = True
        with self.assertRaises(UserError) as invalid:
            decision.test()
        self.assertEqual(invalid.exception.key, "decision_bad_format")

    def test_untrusted_issuer_is_rejected_without_sending_http_request(self):
        context = tls.context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        with self.assertRaises(urllib.error.URLError) as rejected:
            urllib.request.urlopen(self.api.base_url + "/models", context=context, timeout=3)
        self.assertIsInstance(rejected.exception.reason, ssl.SSLCertVerificationError)
        with self.assertRaises(UserError) as product_error:
            relay.list_remote({"base_url": self.api.base_url, "api_key": "qa-local-credential"})
        self.assertEqual(product_error.exception.key, "relay_unreachable")
        # Apple Security and OpenSSL use different messages for the same
        # SSLCertVerificationError; the type and zero HTTP requests are decisive.
        self.assertIn("certificate", product_error.exception.params["detail"].lower())
        self.assertEqual(self.api.requests, [])

    def test_trusted_issuer_wrong_hostname_is_rejected(self):
        self.trust_fixture()
        with self.assertRaises(urllib.error.URLError) as rejected:
            urllib.request.urlopen(self.api.wrong_host_url + "/models", context=tls.context(), timeout=3)
        self.assertIsInstance(rejected.exception.reason, ssl.SSLCertVerificationError)
        self.assertRegex(str(rejected.exception.reason).lower(), r"mismatch|does not match|hostname")
        with self.assertRaises(UserError) as product_error:
            relay.list_remote({"base_url": self.api.wrong_host_url, "api_key": "qa-local-credential"})
        self.assertEqual(product_error.exception.key, "relay_unreachable")
        self.assertEqual(self.api.requests, [])

    def test_openssl_fallback_loads_certifi_when_default_ca_loading_is_missing(self):
        # This specifically exercises the OpenSSL fallback; native truststore
        # contexts do not implement CA enumeration. Native SDK handshakes and
        # certificate rejection are covered by the other tests on the real OS.
        with mock.patch.object(tls.sys, "platform", "linux"), \
                mock.patch.object(ssl.SSLContext, "load_default_certs", return_value=None):
            context = tls.context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreater(len(context.get_ca_certs()), 50, "bundled certifi roots were not loaded")
        # A disposable root stands in for certifi contents to prove a real
        # handshake uses that explicit bundle even when default loading does none.
        with mock.patch.object(tls.sys, "platform", "linux"), \
                mock.patch.object(ssl.SSLContext, "load_default_certs", return_value=None), \
                mock.patch.object(tls.certifi, "where", return_value=str(self.api.ca_file)):
            with urllib.request.urlopen(self.api.base_url + "/models", context=tls.context(), timeout=3) as response:
                self.assertEqual(json.load(response)["data"][0]["id"], self.api.model)

    def test_native_context_factory_keeps_env_cert_file_directory_and_verification(self):
        # The OS trust-store integration itself is exercised by native package
        # smoke tests; this checks factory selection and additive explicit paths.
        import truststore
        self.trust_fixture()
        with mock.patch.dict(os.environ, {"SSL_CERT_DIR": str(self.api.directory)}):
            for platform in ("darwin", "win32"):
                with self.subTest(platform=platform), mock.patch.object(tls.sys, "platform", platform), \
                        mock.patch.object(truststore, "SSLContext", side_effect=ssl.SSLContext) as factory:
                    context = tls.context()
                    factory.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
                    self.assertTrue(context.check_hostname)
                    self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                    with urllib.request.urlopen(self.api.base_url + "/models", context=context, timeout=3) as response:
                        self.assertEqual(response.status, 200)

    def test_real_openai_sdk_models_test_and_structured_completion(self):
        self.trust_fixture()
        cfg = self.config()
        self.assertEqual(openai_compat.list_models(cfg), [self.api.model])
        self.assertEqual(openai_compat.test(cfg)["model"], self.api.model)
        result = openai_compat.complete(cfg, LLMRequest(system="Return JSON probabilities.",
                     messages=[{"role": "user", "content": "buyer or seller?"}], json_schema={"type": "object"}))
        self.assertEqual(json.loads(result.text)["probabilities"][0]["option"], "buyer")
        self.assertGreater(result.input_tokens, 0)

    def test_real_anthropic_sdk_models_test_and_streamed_completion(self):
        self.trust_fixture()
        cfg = self.config(provider="claude", base_url=self.api.root_url)
        self.assertEqual(claude.list_models(cfg), [self.api.model])
        self.assertEqual(claude.test(cfg)["model"], self.api.model)
        result = claude.complete(cfg, LLMRequest(system="Reply OK.",
                               messages=[{"role": "user", "content": "ping"}], max_tokens=5))
        self.assertEqual(result.text, "OK")
        self.assertEqual(result.input_tokens, 5)
        self.assertEqual(result.output_tokens, 1)

    def test_sdk_paths_reject_untrusted_and_wrong_hostname(self):
        for adapter, provider in ((openai_compat, "openai_compat"), (claude, "claude")):
            with self.subTest(provider=provider, issue="untrusted"):
                url = self.api.root_url if provider == "claude" else self.api.base_url
                with self.assertRaises(UserError) as rejected:
                    adapter.list_models(self.config(provider, url))
                self.assertEqual(rejected.exception.key, "network_error")
            with self.subTest(provider=provider, issue="wrong_hostname"), \
                    mock.patch.dict(os.environ, {"SSL_CERT_FILE": str(self.api.ca_file)}):
                url = self.api.wrong_host_url.removesuffix("/v1") if provider == "claude" else self.api.wrong_host_url
                with self.assertRaises(UserError) as rejected:
                    adapter.list_models(self.config(provider, url))
                self.assertEqual(rejected.exception.key, "network_error")
        self.assertEqual(self.api.requests, [])

    def test_openrouter_decisions_https_uses_same_trusted_context(self):
        self.trust_fixture()
        with mock.patch.object(decision, "OPENROUTER_URL", self.api.root_url + "/api/alpha/decisions"):
            probs, _ = decision._openrouter("Buyer request", {"buyer": "buyer", "seller": "seller"},
                                          "Choose one", self.api.model, "qa-local-credential")
        self.assertEqual(probs["buyer"], .95)
        self.assertEqual(self.api.requests[-1]["path"], "/api/alpha/decisions")


if __name__ == "__main__":
    unittest.main()
