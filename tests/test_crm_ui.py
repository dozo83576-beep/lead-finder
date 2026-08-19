import logging
import os
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

from crm import CRMStore
from lead_finder import Lead
from outreach import OutreachStore
from storage import LeadStore


class CRMStreamlitTests(unittest.TestCase):
    def test_crm_mode_renders_four_sections_and_lead_button_creates_no_deal_or_consent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "app.db")
            log_path = os.path.join(tmp, "app.log")
            lead = Lead(
                name="Тестовая компания",
                lead_key="crm-ui-lead",
                city="Екатеринбург",
                email="owner@example.ru",
                source="OpenStreetMap",
            )
            LeadStore(db_path).upsert_many([lead])
            old_values = {name: os.environ.get(name) for name in ("LEAD_DB_PATH", "LEAD_LOG_PATH")}
            os.environ.update({"LEAD_DB_PATH": db_path, "LEAD_LOG_PATH": log_path})
            try:
                app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=15).run()
                app.button(key="crm_add_lead_crm-ui-lead").click().run()

                self.assertFalse(app.exception)
                self.assertEqual(CRMStore(db_path).list_deals(), [])
                permission = OutreachStore(db_path).get_permission(
                    lead.lead_key, "email", lead.email
                )
                self.assertEqual(permission["status"], "unknown")

                app.radio(key="lead_outreach_mode").set_value("Привлечение и CRM").run()

                self.assertFalse(app.exception)
                tab_labels = {tab.label for tab in app.tabs}
                self.assertTrue({"Площадки", "Партнёры", "Сделки", "Финансы"} <= tab_labels)
            finally:
                logging.shutdown()
                for name, value in old_values.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
