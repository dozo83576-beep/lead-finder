import csv
import io
import os
import tempfile
import unittest

from openpyxl import Workbook, load_workbook

from lead_finder import (
    Lead,
    WebsiteAudit,
    apply_pagespeed_result,
    export_csv_bytes,
    export_xlsx_bytes,
    import_csv,
    import_xlsx,
)


class LeadFinderFileTests(unittest.TestCase):
    def test_pagespeed_result_sets_mobile_score(self):
        audit = WebsiteAudit(state="reachable", normalized_url="https://slow.ru")

        result = apply_pagespeed_result(
            audit,
            {"lighthouseResult": {"categories": {"performance": {"score": 0.42}}}},
        )

        self.assertEqual(result.mobile_score, 42)

    def test_import_csv_reads_known_columns_and_builds_stable_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "manual.csv")
            with open(path, "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["name", "phone", "website"])
                writer.writeheader()
                writer.writerow({"name": "Мастер окон", "phone": "+7 343", "website": ""})

            leads = import_csv(path)

        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0].name, "Мастер окон")
        self.assertTrue(leads[0].lead_key.startswith("import:"))

    def test_import_csv_requires_name_column(self):
        data = io.BytesIO(b"website\nexample.ru\n")

        with self.assertRaises(ValueError):
            import_csv(data)

    def test_import_xlsx_reads_first_sheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "manual.xlsx")
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["name", "category", "phone"])
            sheet.append(["Сантехник", "plumber", "+7"])
            workbook.save(path)
            workbook.close()

            leads = import_xlsx(path)

        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0].category, "plumber")

    def test_import_xlsx_requires_name_column(self):
        data = io.BytesIO()
        workbook = Workbook()
        workbook.active.append(["website"])
        workbook.active.append(["example.ru"])
        workbook.save(data)
        workbook.close()
        data.seek(0)

        with self.assertRaises(ValueError):
            import_xlsx(data)

    def test_csv_export_sorts_by_score_and_contains_reasons(self):
        leads = [
            Lead(name="Низкий", lead_key="1", score=10, reasons=["есть телефон"]),
            Lead(name="Высокий", lead_key="2", score=80, reasons=["сайт не указан в источнике"]),
        ]

        rows = list(csv.DictReader(io.StringIO(export_csv_bytes(leads).decode("utf-8-sig"))))

        self.assertEqual(rows[0]["name"], "Высокий")
        self.assertEqual(rows[0]["reasons"], "сайт не указан в источнике")

    def test_xlsx_export_contains_rows(self):
        data = export_xlsx_bytes([Lead(name="Компания", lead_key="1", score=70)])
        workbook = load_workbook(io.BytesIO(data), read_only=True)
        try:
            rows = list(workbook.active.iter_rows(values_only=True))
        finally:
            workbook.close()

        self.assertEqual(rows[0][0], "score")
        self.assertEqual(rows[1][3], "Компания")


if __name__ == "__main__":
    unittest.main()
