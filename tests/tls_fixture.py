"""Disposable HTTPS model API for source and frozen-package TLS checks.

    with TLSFixture() as api:
        env = dict(os.environ, SSL_CERT_FILE=str(api.ca_file))
        # api.base_url is https://localhost:<port>/v1, with a localhost-only cert.

All certificates and the server key are generated at runtime and removed on exit.
No external network, real credential or provider call is involved.
"""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import tempfile
import threading
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


class TLSFixture:
    model = "qa-tls-model"

    def __init__(self):
        self.requests = []
        self.malformed_decision = False
        self._lock = threading.Lock()

    def __enter__(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="jamesagent-tls-")
        self.directory = Path(self._temporary.name)
        self.ca_file = self.directory / "ca.pem"
        key_file = self.directory / "server.key"
        certificate_file = self.directory / "server.pem"
        now = datetime.now(timezone.utc)
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "JamesAgent ephemeral QA CA")])
        ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
              .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                             key_encipherment=False, data_encipherment=False, key_agreement=False,
                             key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
              .sign(ca_key, hashes.SHA256()))
        key = ec.generate_private_key(ec.SECP256R1())
        certificate = (x509.CertificateBuilder()
                       .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
                       .issuer_name(ca_name).public_key(key.public_key())
                       .serial_number(x509.random_serial_number())
                       .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1))
                       .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                       .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                       .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                       .sign(ca_key, hashes.SHA256()))
        self.ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        key_file.chmod(0o600)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def respond(self, value, status=200, content_type="application/json"):
                body = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                self.wfile.write(body)

            def record(self, body=None):
                with owner._lock:
                    owner.requests.append({"method": self.command, "path": self.path, "body": body})

            def do_GET(self):
                self.record()
                path = urlsplit(self.path).path
                model = {"id": owner.model, "object": "model", "type": "model", "created": 0,
                         "created_at": "2026-01-01T00:00:00Z", "display_name": "TLS QA model",
                         "owned_by": "local-qa", "supported_parameters": ["response_format"]}
                if path in ("/models", "/v1/models"):
                    self.respond({"object": "list", "data": [model], "has_more": False,
                                  "first_id": owner.model, "last_id": owner.model})
                elif path == "/v1/models/" + owner.model:
                    self.respond(model)
                else:
                    self.respond({"error": {"message": "Unknown local fixture path"}}, status=404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                self.record(body)
                path = urlsplit(self.path).path
                if path in ("/chat/completions", "/v1/chat/completions"):
                    answer = {"probabilities": [{"option": "buyer", "probability": .95},
                                                {"option": "seller", "probability": .04},
                                                {"option": "other", "probability": .01}]}
                    content = "OK" if owner.malformed_decision else json.dumps(answer)
                    self.respond({"id": "chatcmpl-local-tls", "object": "chat.completion", "created": 0,
                                  "model": body.get("model", owner.model), "choices": [{"index": 0,
                                  "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                                  "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9}})
                elif path == "/v1/messages":
                    message = {"id": "msg_local_tls", "type": "message", "role": "assistant", "content": [],
                               "model": body.get("model", owner.model), "stop_reason": None, "stop_sequence": None,
                               "usage": {"input_tokens": 5, "output_tokens": 0}}
                    events = [
                        ("message_start", {"type": "message_start", "message": message}),
                        ("content_block_start", {"type": "content_block_start", "index": 0,
                                                 "content_block": {"type": "text", "text": ""}}),
                        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                 "delta": {"type": "text_delta", "text": "OK"}}),
                        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn",
                                                                               "stop_sequence": None},
                                           "usage": {"output_tokens": 1}}),
                        ("message_stop", {"type": "message_stop"}),
                    ]
                    wire = "".join(f"event: {name}\ndata: {json.dumps(value)}\n\n" for name, value in events)
                    self.respond(wire.encode(), content_type="text/event-stream")
                elif path == "/api/alpha/decisions":
                    self.respond({"model": body.get("model", owner.model),
                                  "answers": {"q": {"probabilities": {"buyer": .95, "seller": .04, "other": .01}}},
                                  "usage": {"input_tokens": 5, "output_tokens": 4, "cost": 0}})
                else:
                    self.respond({"error": {"message": "Unknown local fixture path"}}, status=404)

        class LocalServer(ThreadingHTTPServer):
            daemon_threads = True

        self.server = LocalServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(certificate_file), str(key_file))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.root_url = f"https://localhost:{self.port}"
        self.base_url = self.root_url + "/v1"
        self.wrong_host_url = f"https://127.0.0.1:{self.port}/v1"
        self._thread = threading.Thread(target=self.server.serve_forever, name="qa-https", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)
        self._temporary.cleanup()
