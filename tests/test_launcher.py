import unittest
from pathlib import Path


class LauncherTests(unittest.TestCase):
    def test_desktop_launcher_loads_yandex_user_variables_before_start(self):
        script = (Path(__file__).parents[1] / "start_lead_finder.ps1").read_text(encoding="utf-8")

        key_lookup = "GetEnvironmentVariable('YANDEX_SEARCH_API_KEY', 'User')"
        folder_lookup = "GetEnvironmentVariable('YANDEX_FOLDER_ID', 'User')"
        process_start = "$process = Start-Process"

        self.assertIn(key_lookup, script)
        self.assertIn(folder_lookup, script)
        self.assertLess(script.index(key_lookup), script.index(process_start))
        self.assertLess(script.index(folder_lookup), script.index(process_start))

    def test_desktop_launcher_loads_outreach_secrets_before_start(self):
        script = (Path(__file__).parents[1] / "start_lead_finder.ps1").read_text(encoding="utf-8")
        process_start = "$process = Start-Process"
        for variable_name in (
            "UNISENDER_API_KEY",
            "OUTREACH_IMAP_PASSWORD",
            "TELEGRAM_BOT_TOKEN",
            "OUTREACH_LINK_SECRET",
        ):
            self.assertIn(f"'{variable_name}'", script)
            self.assertLess(script.index(f"'{variable_name}'"), script.index(process_start))


if __name__ == "__main__":
    unittest.main()
