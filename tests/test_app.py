import os
import tempfile
import unittest
import logging
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from lead_finder import Lead, WebsiteAudit
from storage import LeadStore
from verification import VerificationResult, domain_verification_key


class LeadFinderAppTests(unittest.TestCase):
    def test_yandex_connection_button_shows_success_without_counting_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "app.db")
            log_path = os.path.join(tmp, "app.log")
            old_values = {
                name: os.environ.get(name)
                for name in ("LEAD_DB_PATH", "LEAD_LOG_PATH", "YANDEX_SEARCH_API_KEY", "YANDEX_FOLDER_ID")
            }
            os.environ.update(
                {
                    "LEAD_DB_PATH": db_path,
                    "LEAD_LOG_PATH": log_path,
                    "YANDEX_SEARCH_API_KEY": "secret",
                    "YANDEX_FOLDER_ID": "folder",
                }
            )
            try:
                with patch(
                    "verification.check_yandex_connection",
                    return_value=(True, "Подключение работает.", 1),
                ) as check:
                    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=15).run()
                    app.button(key="check_yandex_connection").click().run()

                self.assertFalse(app.exception)
                self.assertTrue(any("Подключение работает" in item.value for item in app.success))
                self.assertEqual(LeadStore(db_path).monthly_yandex_requests(), 0)
                check.assert_called_once_with("secret", "folder")
            finally:
                logging.shutdown()
                for name, value in old_values.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_yandex_connection_button_is_disabled_in_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_values = {
                name: os.environ.get(name)
                for name in ("LEAD_DB_PATH", "LEAD_LOG_PATH", "YANDEX_SEARCH_API_KEY", "YANDEX_FOLDER_ID")
            }
            os.environ.update(
                {
                    "LEAD_DB_PATH": os.path.join(tmp, "app.db"),
                    "LEAD_LOG_PATH": os.path.join(tmp, "app.log"),
                    "YANDEX_SEARCH_API_KEY": "secret",
                    "YANDEX_FOLDER_ID": "folder",
                }
            )
            try:
                with patch("verification.check_yandex_connection") as check:
                    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=15).run()
                    app.checkbox(key="dry_run").check().run()

                self.assertTrue(app.button(key="check_yandex_connection").disabled)
                check.assert_not_called()
            finally:
                logging.shutdown()
                for name, value in old_values.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_manual_domain_search_replaces_site_and_counts_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "app.db")
            log_path = os.path.join(tmp, "app.log")
            LeadStore(db_path).upsert_many(
                [
                    Lead(
                        name="Урсула",
                        lead_key="working",
                        city="Екатеринбург",
                        website="http://www.ursula.ru",
                        website_source="OpenStreetMap",
                        verification_status="source_provided",
                        audit=WebsiteAudit(state="reachable", normalized_url="http://www.ursula.ru"),
                    )
                ]
            )
            old_values = {
                name: os.environ.get(name)
                for name in ("LEAD_DB_PATH", "LEAD_LOG_PATH", "YANDEX_SEARCH_API_KEY", "YANDEX_FOLDER_ID")
            }
            os.environ.update(
                {
                    "LEAD_DB_PATH": db_path,
                    "LEAD_LOG_PATH": log_path,
                    "YANDEX_SEARCH_API_KEY": "secret",
                    "YANDEX_FOLDER_ID": "folder",
                }
            )

            def fake_enrich(leads, **_kwargs):
                leads[0].audit = WebsiteAudit(state="reachable", normalized_url=leads[0].website)
                return leads

            try:
                with (
                    patch(
                        "verification.verify_lead_site",
                        return_value=VerificationResult(
                            "site_found",
                            "https://www.ursula.pro",
                            ["на странице совпал телефон компании"],
                            1,
                        ),
                    ),
                    patch("lead_finder.enrich_leads", side_effect=fake_enrich),
                ):
                    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=15).run()
                    app.button(key="find_current_site_working").click().run()

                saved = LeadStore(db_path).list_leads()[0]
                self.assertFalse(app.exception)
                self.assertEqual(saved.website, "https://www.ursula.pro")
                self.assertEqual(saved.website_source, "Yandex Search API")
                self.assertIn("http://www.ursula.ru", " ".join(saved.verification_evidence))
                self.assertEqual(LeadStore(db_path).monthly_yandex_requests(), 1)
            finally:
                logging.shutdown()
                for name, value in old_values.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_manual_domain_search_does_not_replace_same_domain_without_scheme(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "app.db")
            LeadStore(db_path).upsert_many(
                [
                    Lead(
                        name="Компания",
                        lead_key="same-domain",
                        city="Екатеринбург",
                        website="company.ru",
                        website_source="OpenStreetMap",
                        verification_status="source_provided",
                        audit=WebsiteAudit(state="reachable", normalized_url="https://company.ru"),
                    )
                ]
            )
            old_values = {
                name: os.environ.get(name)
                for name in ("LEAD_DB_PATH", "LEAD_LOG_PATH", "YANDEX_SEARCH_API_KEY", "YANDEX_FOLDER_ID")
            }
            os.environ.update(
                {
                    "LEAD_DB_PATH": db_path,
                    "LEAD_LOG_PATH": os.path.join(tmp, "app.log"),
                    "YANDEX_SEARCH_API_KEY": "secret",
                    "YANDEX_FOLDER_ID": "folder",
                }
            )
            try:
                with (
                    patch(
                        "verification.verify_lead_site",
                        return_value=VerificationResult(
                            "site_found",
                            "https://www.company.ru",
                            ["на странице совпало название компании"],
                            1,
                        ),
                    ),
                    patch("lead_finder.enrich_leads", side_effect=lambda leads, **_kwargs: leads),
                ):
                    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=15).run()
                    app.button(key="find_current_site_same-domain").click().run()

                saved = LeadStore(db_path).list_leads()[0]
                history = LeadStore(db_path).list_search_runs()
                self.assertFalse(app.exception)
                self.assertEqual(saved.website, "company.ru")
                self.assertEqual(saved.website_source, "OpenStreetMap")
                self.assertEqual(history[0]["sites_found"], 0)
            finally:
                logging.shutdown()
                for name, value in old_values.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_manual_domain_search_uses_cache_without_credentials_or_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "app.db")
            store = LeadStore(db_path)
            lead = Lead(
                name="Урсула",
                lead_key="cached",
                city="Екатеринбург",
                phone="+7 343 223-04-73",
                website="http://www.ursula.ru",
                website_source="OpenStreetMap",
                verification_status="source_provided",
                audit=WebsiteAudit(state="reachable", normalized_url="http://www.ursula.ru"),
            )
            store.upsert_many([lead])
            store.save_domain_verification(
                domain_verification_key(lead),
                "site_found",
                "https://www.ursula.pro",
                ["совпал телефон"],
            )
            old_values = {
                name: os.environ.get(name)
                for name in ("LEAD_DB_PATH", "LEAD_LOG_PATH", "YANDEX_SEARCH_API_KEY", "YANDEX_FOLDER_ID")
            }
            os.environ["LEAD_DB_PATH"] = db_path
            os.environ["LEAD_LOG_PATH"] = os.path.join(tmp, "app.log")
            os.environ.pop("YANDEX_SEARCH_API_KEY", None)
            os.environ.pop("YANDEX_FOLDER_ID", None)
            try:
                with (
                    patch("verification.verify_lead_site") as network_check,
                    patch("lead_finder.enrich_leads", side_effect=lambda leads, **_kwargs: leads),
                ):
                    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=15).run()
                    app.button(key="find_current_site_cached").click().run()

                saved_store = LeadStore(db_path)
                saved = saved_store.list_leads()[0]
                history = saved_store.list_search_runs()
                self.assertFalse(app.exception)
                self.assertEqual(saved.website, "https://www.ursula.pro")
                self.assertEqual(saved_store.monthly_yandex_requests(), 0)
                self.assertEqual(history[0]["cache_hits"], 1)
                self.assertTrue(
                    any("Из кэша" in dataframe.value.columns for dataframe in app.dataframe)
                )
                network_check.assert_not_called()
            finally:
                logging.shutdown()
                for name, value in old_values.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_force_domain_search_ignores_cache_and_counts_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "app.db")
            store = LeadStore(db_path)
            lead = Lead(
                name="Урсула",
                lead_key="force",
                city="Екатеринбург",
                phone="+7 343 223-04-73",
                website="http://www.ursula.ru",
                website_source="OpenStreetMap",
                verification_status="source_provided",
                audit=WebsiteAudit(state="reachable", normalized_url="http://www.ursula.ru"),
            )
            store.upsert_many([lead])
            store.save_domain_verification(
                domain_verification_key(lead),
                "likely_no_site",
                "",
                ["старый результат"],
            )
            old_values = {
                name: os.environ.get(name)
                for name in ("LEAD_DB_PATH", "LEAD_LOG_PATH", "YANDEX_SEARCH_API_KEY", "YANDEX_FOLDER_ID")
            }
            os.environ.update(
                {
                    "LEAD_DB_PATH": db_path,
                    "LEAD_LOG_PATH": os.path.join(tmp, "app.log"),
                    "YANDEX_SEARCH_API_KEY": "secret",
                    "YANDEX_FOLDER_ID": "folder",
                }
            )
            try:
                with (
                    patch(
                        "verification.verify_lead_site",
                        return_value=VerificationResult(
                            "site_found",
                            "https://www.ursula.pro",
                            ["совпал телефон"],
                            1,
                        ),
                    ),
                    patch("lead_finder.enrich_leads", side_effect=lambda leads, **_kwargs: leads),
                ):
                    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=15).run()
                    app.button(key="refresh_current_site_force").click().run()

                saved_store = LeadStore(db_path)
                history = saved_store.list_search_runs()
                self.assertFalse(app.exception)
                self.assertEqual(saved_store.list_leads()[0].website, "https://www.ursula.pro")
                self.assertEqual(saved_store.monthly_yandex_requests(), 1)
                self.assertEqual(history[0]["cache_hits"], 0)
                self.assertEqual(
                    saved_store.get_domain_verification(domain_verification_key(lead))["website"],
                    "https://www.ursula.pro",
                )
            finally:
                logging.shutdown()
                for name, value in old_values.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_search_audits_old_site_before_yandex_and_reaudits_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "app.db")
            log_path = os.path.join(tmp, "app.log")
            store = LeadStore(db_path)
            store.save_city_bbox("Екатеринбург", (56.7, 60.4, 56.9, 60.8))
            old_values = {
                name: os.environ.get(name)
                for name in ("LEAD_DB_PATH", "LEAD_LOG_PATH", "YANDEX_SEARCH_API_KEY", "YANDEX_FOLDER_ID")
            }
            os.environ.update(
                {
                    "LEAD_DB_PATH": db_path,
                    "LEAD_LOG_PATH": log_path,
                    "YANDEX_SEARCH_API_KEY": "secret",
                    "YANDEX_FOLDER_ID": "folder",
                }
            )
            lead = Lead(
                name="Урсула",
                lead_key="osm:node:1",
                city="Екатеринбург",
                phone="+7 343 223-04-73",
                website="http://www.ursula.ru",
                website_source="OpenStreetMap",
                verification_status="source_provided",
            )
            enrich_calls: list[str] = []

            def fake_enrich(leads, **_kwargs):
                enrich_calls.append(leads[0].website)
                state = "broken" if leads[0].website.endswith("ursula.ru") else "reachable"
                leads[0].audit = WebsiteAudit(state=state, normalized_url=leads[0].website)
                return leads

            def fake_verify(leads, *_args, **_kwargs):
                if leads[0].audit is not None and leads[0].audit.state == "broken":
                    leads[0].website = "https://www.ursula.pro"
                    leads[0].website_source = "Yandex Search API"
                    leads[0].verification_status = "site_found"
                    leads[0].verification_evidence.append(
                        "старый сайт из OpenStreetMap: http://www.ursula.ru"
                    )
                    leads[0].audit = None
                    return leads, {"yandex_checked": 1, "sites_found": 1, "api_requests": 1}
                return leads, {"yandex_checked": 0, "sites_found": 0, "api_requests": 0}

            try:
                with (
                    patch("lead_finder.collect_osm", return_value=[lead]),
                    patch("lead_finder.enrich_leads", side_effect=fake_enrich),
                    patch("verification.verify_missing_leads", side_effect=fake_verify),
                ):
                    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=15).run()
                    app.button(key="search").click().run()

                saved = LeadStore(db_path).list_leads()[0]
                self.assertFalse(app.exception)
                self.assertEqual(saved.website, "https://www.ursula.pro")
                self.assertEqual(saved.audit.state, "reachable")
                self.assertEqual(
                    enrich_calls,
                    ["http://www.ursula.ru", "https://www.ursula.pro"],
                )
            finally:
                logging.shutdown()
                for name, value in old_values.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_search_rescores_likely_no_site_after_yandex_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "app.db")
            store = LeadStore(db_path)
            store.save_city_bbox("Екатеринбург", (56.7, 60.4, 56.9, 60.8))
            old_values = {
                name: os.environ.get(name)
                for name in ("LEAD_DB_PATH", "LEAD_LOG_PATH", "YANDEX_SEARCH_API_KEY", "YANDEX_FOLDER_ID")
            }
            os.environ.update(
                {
                    "LEAD_DB_PATH": db_path,
                    "LEAD_LOG_PATH": os.path.join(tmp, "app.log"),
                    "YANDEX_SEARCH_API_KEY": "secret",
                    "YANDEX_FOLDER_ID": "folder",
                }
            )
            lead = Lead(
                name="Без сайта",
                lead_key="osm:node:2",
                city="Екатеринбург",
                phone="+7 343 000-00-00",
                verification_status="ambiguous",
            )

            def fake_enrich(leads, **_kwargs):
                leads[0].audit = WebsiteAudit(state="missing")
                leads[0].need_score = 0
                leads[0].contact_score = 20
                leads[0].score = 20
                return leads

            def fake_verify(leads, *_args, **_kwargs):
                leads[0].verification_status = "likely_no_site"
                return leads, {"yandex_checked": 1, "sites_found": 0, "api_requests": 1}

            try:
                with (
                    patch("lead_finder.collect_osm", return_value=[lead]),
                    patch("lead_finder.enrich_leads", side_effect=fake_enrich),
                    patch("verification.verify_missing_leads", side_effect=fake_verify),
                ):
                    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=15).run()
                    app.button(key="search").click().run()

                saved = LeadStore(db_path).list_leads()[0]
                self.assertFalse(app.exception)
                self.assertEqual(saved.verification_status, "likely_no_site")
                self.assertEqual(saved.need_score, 45)
                self.assertEqual(saved.score, 65)
            finally:
                logging.shutdown()
                for name, value in old_values.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_dry_run_renders_leads_without_saving_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "app.db")
            log_path = os.path.join(tmp, "app.log")
            old_db = os.environ.get("LEAD_DB_PATH")
            old_log = os.environ.get("LEAD_LOG_PATH")
            os.environ["LEAD_DB_PATH"] = db_path
            os.environ["LEAD_LOG_PATH"] = log_path
            try:
                app_path = Path(__file__).parents[1] / "app.py"
                with patch("storage.LeadStore.get_domain_verification") as cache_read:
                    app = AppTest.from_file(app_path, default_timeout=15).run()
                    self.assertEqual(app.title[0].value, "Lead Finder")
                    self.assertTrue(any("около 24.40 ₽" in caption.value for caption in app.caption))

                    app.checkbox(key="dry_run").check().run()
                    app.button(key="search").click().run()

                cache_read.assert_not_called()

                self.assertFalse(app.exception)
                self.assertIn("Мастер окон", app.dataframe[0].value["Компания"].tolist())
                self.assertEqual(len(app.code), 3)
                self.assertEqual(LeadStore(db_path).list_leads(), [])
            finally:
                logging.shutdown()
                if old_db is None:
                    os.environ.pop("LEAD_DB_PATH", None)
                else:
                    os.environ["LEAD_DB_PATH"] = old_db
                if old_log is None:
                    os.environ.pop("LEAD_LOG_PATH", None)
                else:
                    os.environ["LEAD_LOG_PATH"] = old_log

    def test_import_button_without_file_warns_and_does_not_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "app.db")
            log_path = os.path.join(tmp, "app.log")
            old_db = os.environ.get("LEAD_DB_PATH")
            old_log = os.environ.get("LEAD_LOG_PATH")
            os.environ["LEAD_DB_PATH"] = db_path
            os.environ["LEAD_LOG_PATH"] = log_path
            try:
                with patch("lead_finder.import_csv") as import_call:
                    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=15).run()
                    app.button(key="import").click().run()

                import_call.assert_not_called()

                self.assertFalse(app.exception)
                self.assertTrue(any("Выберите CSV или XLSX" in item.value for item in app.warning))
                self.assertEqual(LeadStore(db_path).list_leads(), [])
            finally:
                logging.shutdown()
                if old_db is None:
                    os.environ.pop("LEAD_DB_PATH", None)
                else:
                    os.environ["LEAD_DB_PATH"] = old_db
                if old_log is None:
                    os.environ.pop("LEAD_LOG_PATH", None)
                else:
                    os.environ["LEAD_LOG_PATH"] = old_log

    def test_three_queues_and_manual_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "app.db")
            log_path = os.path.join(tmp, "app.log")
            store = LeadStore(db_path)
            store.upsert_many(
                [
                    Lead(
                        name="Нужно подтвердить",
                        lead_key="pending",
                        city="Екатеринбург",
                        phone="+7",
                        verification_status="likely_no_site",
                        need_score=45,
                        contact_score=20,
                        score=65,
                        audit=WebsiteAudit(state="missing"),
                    )
                ]
            )
            old_db = os.environ.get("LEAD_DB_PATH")
            old_log = os.environ.get("LEAD_LOG_PATH")
            os.environ["LEAD_DB_PATH"] = db_path
            os.environ["LEAD_LOG_PATH"] = log_path
            try:
                app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=15).run()

                self.assertEqual(
                    [tab.label for tab in app.tabs[:3]],
                    ["Готовые лиды", "Требуют подтверждения", "Все"],
                )
                app.button(key="confirm_no_site_pending").click().run()
                saved = LeadStore(db_path).list_leads()[0]

                self.assertFalse(app.exception)
                self.assertEqual(saved.verification_status, "confirmed_no_site")
                self.assertEqual(saved.need_score, 70)
            finally:
                logging.shutdown()
                if old_db is None:
                    os.environ.pop("LEAD_DB_PATH", None)
                else:
                    os.environ["LEAD_DB_PATH"] = old_db
                if old_log is None:
                    os.environ.pop("LEAD_LOG_PATH", None)
                else:
                    os.environ["LEAD_LOG_PATH"] = old_log

    def test_manual_website_is_saved_as_verified_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "app.db")
            log_path = os.path.join(tmp, "app.log")
            LeadStore(db_path).upsert_many(
                [Lead(name="Компания", lead_key="manual", verification_status="likely_no_site", audit=WebsiteAudit(state="missing"))]
            )
            old_db = os.environ.get("LEAD_DB_PATH")
            old_log = os.environ.get("LEAD_LOG_PATH")
            os.environ["LEAD_DB_PATH"] = db_path
            os.environ["LEAD_LOG_PATH"] = log_path
            try:
                app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=15).run()
                app.text_input(key="manual_site_manual").set_value("http://127.0.0.1:1")
                app.button(key="save_site_manual").click().run()
                saved = LeadStore(db_path).list_leads()[0]

                self.assertFalse(app.exception)
                self.assertEqual(saved.verification_status, "site_found")
                self.assertEqual(saved.website, "http://127.0.0.1:1")
            finally:
                logging.shutdown()
                if old_db is None:
                    os.environ.pop("LEAD_DB_PATH", None)
                else:
                    os.environ["LEAD_DB_PATH"] = old_db
                if old_log is None:
                    os.environ.pop("LEAD_LOG_PATH", None)
                else:
                    os.environ["LEAD_LOG_PATH"] = old_log


if __name__ == "__main__":
    unittest.main()
