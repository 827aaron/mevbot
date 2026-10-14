"""Public iGaming agent-feature candidates; no new market or quality narrowing."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import test_app as app_tests
from salesteam import db, edition, llm, paths, settings, tasks
from salesteam.agents import analyst, icp as icp_mod, researcher
from salesteam.errors import UserError


class AgentSignalEditionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {"SALESTEAM_EDITION": "my_igaming"}))
        app_tests.AppTests.setUp(self)
        self.counter = 0
        self.profile = icp_mod.normalize({**edition.presets()[0]["data"], "product": "Public B2B partnership services"})
        now = db.now()
        self.icp_id = db.execute("INSERT INTO icp_templates(name,data,created_at,updated_at) VALUES(?,?,?,?)",
                                 (self.profile["name"], db.dumps(self.profile), now, now))
        settings.put("analyst", {"enrich_website": False})

    def make_run(self):
        self.counter += 1
        ident = f"edition-qa-{self.counter}"
        db.execute("INSERT INTO tasks(id,kind,icp_id,params,status,target,created_at) VALUES(?,?,?,?,?,?,?)",
                   (ident, "research", self.icp_id, db.dumps({"sources": ["web"], "target": 10}),
                    "running", 10, db.now()))
        run = tasks.TaskRun(db.query_one("SELECT * FROM tasks WHERE id=?", (ident,)))
        tasks._RUNS[ident] = run
        return run

    def candidate(self, text, registered="SG", forged=None):
        run = self.make_run()
        url = f"https://public-business.example/qa-{self.counter}"
        candidate = {"company_name": f"QA Partner Agency {self.counter}", "website": "https://public-business.example", "country": registered,
                     "customer_type": "affiliate_agency", "description": "Public business partner",
                     "telegram": [f"@qa_business_{self.counter}"], "emails": [], "whatsapp": [],
                     "evidence_quote": text, "page_excerpt": text, "verified_text": True}
        if forged:
            candidate.update(forged)
        rid = db.execute("INSERT INTO raw_leads(task_id,source,source_url,company_name,country,data,evidence,created_at) "
                         "VALUES(?,?,?,?,?,?,?,?)",
                         (run.id, "web", url, candidate["company_name"], registered, db.dumps(candidate), text, db.now()))
        return run, rid, candidate, url

    def score(self, run, rid, candidate, url, grade="A", excluded=False):
        output = {"grade": grade, "score": 95 if grade in ("A", "B") else 10, "grade_reason": "QA model suggests a fit", "country": candidate["country"],
                  "customer_type": "affiliate_agency", "hard_match": {"country_ok": True, "customer_type_ok": True,
                                                                           "excluded": excluded},
                  "sources": [{"url": url, "quote": candidate["evidence_quote"]}], "profile": {"summary": "QA profile"}}
        with mock.patch.object(llm, "call_json", return_value=(output, {})), \
                mock.patch.object(analyst.decision, "enabled", return_value=False), \
                mock.patch.object(analyst.quality, "should_check", return_value=False), \
                mock.patch("socket.socket.connect", side_effect=AssertionError("unexpected enterprise test network")):
            analyst.Analyst(run, self.profile).process(rid)
        raw = db.query_one("SELECT * FROM raw_leads WHERE id=?", (rid,))
        lead = db.query_one("SELECT * FROM leads WHERE id=?", (raw["lead_id"],)) if raw["lead_id"] else None
        return raw, lead

    def test_foreign_registered_business_with_original_my_service_evidence_qualifies(self):
        quote = "We are an iGaming affiliate agency. We serve customers and channel partners in Malaysia."
        run, rid, cand, url = self.candidate(quote, registered="SG")
        raw, lead = self.score(run, rid, cand, url)
        self.assertEqual(raw["status"], "qualified")
        self.assertEqual(lead["qualified"], 1)
        self.assertEqual(lead["country"], "SG", "served market must not rewrite registration country")
        profile = db.loads(lead["profile"], {})
        self.assertEqual(profile["market_validation"]["status"], "matched")
        self.assertEqual(profile["market_validation"]["registered_country"], "SG")
        self.assertEqual(profile["b2b_validation"]["status"], "suspected")
        evidence = profile["market_validation"]["evidence"][0]
        self.assertEqual(evidence["url"], url)
        self.assertIn(evidence["quote"], quote)

    def test_unknown_my_market_remains_candidate_without_inventing_market_evidence(self):
        quote = "We are an iGaming affiliate agency registered in Malaysia. We serve customers in Singapore."
        forged = {"target_markets": ["MY"], "served_markets": ["MY"],
                  "market_evidence": [{"market": "MY", "quote": "We serve customers in Malaysia."}]}
        run, rid, cand, url = self.candidate(quote, registered="MY", forged=forged)
        raw, lead = self.score(run, rid, cand, url)
        self.assertEqual(raw["status"], "qualified")
        self.assertEqual(lead["qualified"], 1)
        profile = db.loads(lead["profile"], {})
        self.assertEqual(profile["market_validation"]["status"], "unverified")
        self.assertEqual(profile["market_validation"]["evidence"], [])
        self.assertEqual(profile["b2b_validation"]["status"], "suspected")

    def test_explicit_non_my_service_is_labeled_without_discarding_agent_feature(self):
        quote = "We are an iGaming affiliate agency. We do not serve Malaysia."
        run, rid, cand, url = self.candidate(quote)
        raw, lead = self.score(run, rid, cand, url)
        self.assertEqual(raw["status"], "qualified")
        self.assertEqual(lead["qualified"], 1)
        profile = db.loads(lead["profile"], {})
        self.assertEqual(profile["market_validation"]["status"], "mismatch")
        self.assertTrue(profile["market_validation"]["negative_evidence"])
        self.assertEqual(profile["b2b_validation"]["status"], "suspected")
        self.assertEqual(run.stats["qualified"], 1)

    def test_individual_consumer_is_excluded_despite_forged_business_type(self):
        # Synthetic exclusion fixture only; no individual contact discovery or outreach.
        quote = "I am a player in Malaysia looking for a bonus."
        run, rid, cand, url = self.candidate(quote, registered="MY")
        raw, lead = self.score(run, rid, cand, url)
        self.assertEqual(raw["status"], "discarded")
        self.assertEqual(raw["reason"], "b2b_mismatch")
        self.assertIsNone(lead)

    def test_non_agent_operator_and_unrelated_directory_text_never_qualify(self):
        quote = "We are an iGaming operator. We serve business customers in Malaysia."
        run, rid, cand, url = self.candidate(quote)
        raw, lead = self.score(run, rid, cand, url)
        self.assertEqual(raw["status"], "discarded")
        self.assertEqual(raw["reason"], "b2b_unverified")
        self.assertIsNone(lead)
        name = f"QA Partner Agency {self.counter + 1}"
        quote = f"{name} publishes public business information. Neighbour Agency is an iGaming affiliate agency serving Malaysia."
        run, rid, cand, _ = self.candidate(quote)
        cand["website"] = "https://candidate-own.example"
        url = "https://directory.example/businesses"
        db.execute("UPDATE raw_leads SET source_url=?,data=? WHERE id=?", (url, db.dumps(cand), rid))
        raw, lead = self.score(run, rid, cand, url)
        self.assertEqual(raw["status"], "discarded")
        self.assertEqual(raw["reason"], "b2b_unverified")
        self.assertIsNone(lead)

    def test_weak_phone_currency_and_server_location_are_not_my_market_evidence(self):
        for text in ("Malaysia", "Contact +60 12 345 6789. Prices in RM.", "We operate servers in Malaysia.",
                     "Our hosting location is a Malaysia data centre."):
            with self.subTest(text=text):
                result = edition.validate_market(self.profile, [{"url": "https://public-business.example/", "text": text}], "MY")
                self.assertEqual(result["status"], "unverified")
                self.assertEqual(result["evidence"], [])

    def test_market_evidence_retains_original_quotes_in_three_languages(self):
        for text in ("We serve business customers in Malaysia.", "我们面向马来西亚市场提供企业服务。",
                     "Kami melayani pelanggan Malaysia."):
            with self.subTest(text=text):
                result = edition.validate_market(self.profile, [{"url": "https://public-business.example/", "text": text}], "SG")
                self.assertEqual(result["status"], "matched")
                self.assertEqual(result["registered_country"], "SG")
                self.assertEqual(result["evidence"][0]["quote"], text)

    def test_disabled_analyst_passthrough_keeps_same_agent_signal_boundary(self):
        for text, expected in (("We are an iGaming affiliate agency. We serve business customers in Malaysia.", "qualified"),
                               ("We are an iGaming affiliate agency. Our registration country is Malaysia.", "qualified"),
                               ("We are an iGaming affiliate agency. We do not serve Malaysia.", "qualified"),
                               ("I am a player in Malaysia.", "filtered")):
            with self.subTest(expected=expected):
                run, rid, _, _ = self.candidate(text)
                analyst.Passthrough(run, self.profile).process(rid)
                raw = db.query_one("SELECT * FROM raw_leads WHERE id=?", (rid,))
                self.assertEqual(raw["status"], expected)
                if raw["lead_id"]:
                    lead = db.query_one("SELECT * FROM leads WHERE id=?", (raw["lead_id"],))
                    self.assertEqual(bool(lead["qualified"]), expected == "qualified")

    def test_email_only_or_no_contact_and_low_model_grade_do_not_narrow_agent_candidates(self):
        for grade, emails in (("C", ["business-contact@qa.example"]), ("D", [])):
            with self.subTest(grade=grade, email_only=bool(emails)):
                quote = "We are an iGaming affiliate agency. Business email business-contact@qa.example."
                run, rid, cand, url = self.candidate(quote, registered="SG",
                                                   forged={"telegram": [], "emails": emails, "whatsapp": []})
                raw, lead = self.score(run, rid, cand, url, grade=grade, excluded=True)
                self.assertEqual(raw["status"], "qualified")
                self.assertEqual(lead["qualified"], 1)
                self.assertEqual(lead["grade"], grade)
                self.assertEqual(db.loads(lead["profile"], {})["b2b_validation"]["status"], "suspected")
                self.assertEqual(db.loads(lead["profile"], {})["market_validation"]["status"], "unverified")
                self.assertTrue(db.loads(lead["sources"], []))
                if not emails:
                    self.assertEqual(db.loads(lead["contacts"], []), [])

    def test_public_professional_individual_agent_feature_is_a_pending_candidate(self):
        # Public professional self-description; no personal gamer discovery or chat.
        quote = "I am an iGaming agent offering business partnerships."
        run, rid, cand, url = self.candidate(quote, registered="SG",
                                           forged={"company_name": "QA Professional Agent", "telegram": [], "emails": []})
        raw, lead = self.score(run, rid, cand, url, grade="D")
        self.assertEqual(raw["status"], "qualified")
        self.assertEqual(lead["qualified"], 1)
        self.assertEqual(lead["company_name"], "QA Professional Agent")
        profile = db.loads(lead["profile"], {})
        self.assertEqual(profile["b2b_validation"]["status"], "suspected")
        self.assertEqual(profile["b2b_validation"]["evidence"][0]["quote"], quote)
        self.assertEqual(profile["market_validation"]["status"], "unverified")

    def test_general_edition_keeps_original_grade_and_contact_quality_rules(self):
        original = dict(self.profile)
        with mock.patch.dict(os.environ, {"SALESTEAM_EDITION": "general"}):
            self.profile = icp_mod.normalize({**original, "geo_mode": "registered_country", "target_markets": [],
                                             "b2b_segment": "", "countries": ["MY"]})
            for grade, expected in (("C", "backup"), ("D", "discarded")):
                with self.subTest(grade=grade):
                    quote = "We are an iGaming affiliate agency serving Malaysia."
                    run, rid, cand, url = self.candidate(quote, registered="MY",
                                                       forged={"telegram": [], "emails": [], "whatsapp": []})
                    raw, lead = self.score(run, rid, cand, url, grade=grade)
                    self.assertEqual(raw["status"], expected)
                    if lead:
                        self.assertEqual(lead["qualified"], 0)
                    else:
                        self.assertEqual(raw["reason"], "grade_d")
        self.profile = original

    def test_presets_need_confirmation_and_never_overwrite_user_configuration_or_edited_template(self):
        optional_product = icp_mod.normalize({**edition.presets()[0]["data"], "product": ""})
        self.assertTrue(optional_product["product"].strip(), "blank product uses the edition's research target")
        before = db.query("SELECT * FROM icp_templates")
        settings.put("budget", {"daily": 7, "monthly": 70})
        settings.put("company_profile", {"name": "User's existing company", "summary": "Preserve my offer"})
        settings.put("search", {"provider": "free", "queries_per_source": 3})
        settings.set_secret("serper_api_key", "synthetic-existing-key")
        settings_snapshot = db.query("SELECT * FROM settings ORDER BY key")
        model_snapshot = db.query("SELECT * FROM agent_configs ORDER BY agent")
        with self.assertRaises(UserError):
            edition.apply_presets(confirmed=False)
        self.assertEqual(db.query("SELECT * FROM icp_templates"), before)
        with mock.patch.object(llm, "call_json", side_effect=AssertionError("preset import must not call AI")), \
                mock.patch("socket.socket.connect", side_effect=AssertionError("unexpected preset network")):
            for invalid_product in (42, True, {}, "x" * 10001):
                with self.subTest(invalid_product=str(invalid_product)[:10]), self.assertRaises(UserError):
                    edition.apply_presets(confirmed=True, product=invalid_product)
            self.assertEqual(db.query("SELECT * FROM icp_templates"), before)
            created = edition.apply_presets(confirmed=True, product="My public enterprise service")
            self.assertEqual(len(created), 1)
            first = created[0]
            self.assertEqual(first["data"]["product"], "My public enterprise service")
            self.assertEqual(first["data"]["b2b_segment"], "channel_team")
            corrected = {**first["data"], "name": "My edited channel team", "product": "My existing offer"}
            db.execute("UPDATE icp_templates SET name=?,data=? WHERE id=?",
                       (corrected["name"], db.dumps(corrected), first["id"]))
            again = edition.apply_presets(confirmed=True, product="Do not overwrite my edited offer")
        edited = next(row for row in again if row["id"] == first["id"])
        self.assertEqual(edited["data"]["name"], corrected["name"])
        self.assertEqual(edited["data"]["product"], corrected["product"])
        after_settings = [row for row in db.query("SELECT * FROM settings ORDER BY key") if row["key"] != "edition_presets_confirmed"]
        self.assertEqual(after_settings, settings_snapshot)
        self.assertEqual(db.query("SELECT * FROM agent_configs ORDER BY agent"), model_snapshot)
        self.assertEqual(settings.get_secret("serper_api_key"), "synthetic-existing-key")
        self.assertEqual(db.query("SELECT * FROM tasks"), [])

    def test_icp_edit_and_chat_preserve_served_market_and_b2b_segment(self):
        edited = {"name": "Edited business profile", "product": "Enterprise services", "customer_types": ["affiliate_agency"]}
        saved = icp_mod.save(edited, self.icp_id)
        for field in ("geo_mode", "target_markets", "b2b_segment"):
            self.assertEqual(saved["data"][field], self.profile[field])
        with mock.patch.object(llm, "call_json", return_value=({"icp": edited, "reply": "QA edited", "changes": []}, {})):
            result = icp_mod.chat(saved["data"], "Edit the name only")
        for field in ("geo_mode", "target_markets", "b2b_segment"):
            self.assertEqual(result["icp"][field], self.profile[field])

    def test_edition_data_directories_are_separate_and_explicit_override_is_honored(self):
        snippet = "import json; from salesteam import edition, desktop, paths; print(json.dumps({'edition':edition.current()['id'],'default':desktop.default_data_dir(),'data':str(paths.DATA_DIR)}))"
        results = {}
        for selected in ("general", "my_igaming"):
            # Importing and selecting a directory is pure: no real user directory is written.
            override = paths.DATA_DIR / ("override-" + selected)
            env = dict(os.environ, SALESTEAM_EDITION=selected, SALESTEAM_DATA=str(override))
            result = subprocess.run([sys.executable, "-c", snippet], cwd=Path(__file__).resolve().parents[1], env=env,
                                    capture_output=True, text=True, timeout=15, check=True)
            results[selected] = json.loads(result.stdout)
            self.assertEqual(results[selected]["edition"], selected)
            self.assertEqual(results[selected]["data"], str(override.resolve()))
        self.assertNotEqual(results["general"]["default"], results["my_igaming"]["default"])
        self.assertEqual(Path(results["general"]["default"]).name, "JamesAgent")
        self.assertEqual(Path(results["my_igaming"]["default"]).name, "JamesAgent-MY-iGaming")
        for selected, expected_dir in (("general", "data"), ("my_igaming", "data-my-igaming")):
            env = dict(os.environ, SALESTEAM_EDITION=selected)
            env.pop("SALESTEAM_DATA", None)
            result = subprocess.run([sys.executable, "-c", snippet], cwd=Path(__file__).resolve().parents[1], env=env,
                                    capture_output=True, text=True, timeout=15, check=True)
            self.assertEqual(Path(json.loads(result.stdout)["data"]).name, expected_dir)


if __name__ == "__main__":
    unittest.main()
