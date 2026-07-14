import os
import tempfile
import unittest
import logging
from pathlib import Path

from streamlit.testing.v1 import AppTest

from storage import LeadStore


class LeadFinderAppTests(unittest.TestCase):
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
                app = AppTest.from_file(app_path, default_timeout=15).run()
                self.assertEqual(app.title[0].value, "Lead Finder")

                app.checkbox(key="dry_run").check().run()
                app.button(key="search").click().run()

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


if __name__ == "__main__":
    unittest.main()
