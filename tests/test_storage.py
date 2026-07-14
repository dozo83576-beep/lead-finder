import os
import sqlite3
import tempfile
import unittest

from lead_finder import Lead, WebsiteAudit
from storage import LeadStore


class LeadStoreTests(unittest.TestCase):
    def test_migration_preserves_existing_status_and_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leads.db")
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE leads (
                    lead_key TEXT PRIMARY KEY, name TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '',
                    address TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '', social TEXT NOT NULL DEFAULT '',
                    website TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '', score INTEGER NOT NULL DEFAULT 0,
                    reasons_json TEXT NOT NULL DEFAULT '[]', audit_json TEXT,
                    status TEXT NOT NULL DEFAULT 'Новый', note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                "INSERT INTO leads (lead_key, name, website, source, status, note) VALUES ('old', 'Старая компания', 'example.ru', 'OpenStreetMap', 'Связался', 'Не потерять')"
            )
            connection.commit()
            connection.close()

            store = LeadStore(path)
            saved = store.list_leads()[0]
            connection = sqlite3.connect(path)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(leads)")}
            connection.close()

        self.assertEqual(saved.status, "Связался")
        self.assertEqual(saved.note, "Не потерять")
        self.assertEqual(saved.verification_status, "source_provided")
        self.assertEqual(saved.website_source, "OpenStreetMap")
        self.assertIn("verification_evidence_json", columns)
        self.assertIn("branch_count", columns)

    def test_city_cache_expires_after_thirty_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leads.db")
            store = LeadStore(path)
            bbox = (56.7, 60.4, 56.9, 60.8)
            store.save_city_bbox("Екатеринбург", bbox)
            self.assertEqual(store.get_city_bbox(" екатеринбург "), bbox)

            connection = sqlite3.connect(path)
            connection.execute("UPDATE city_cache SET updated_at = '2020-01-01T00:00:00+00:00'")
            connection.commit()
            connection.close()

            self.assertIsNone(store.get_city_bbox("Екатеринбург"))

    def test_search_history_counts_monthly_yandex_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LeadStore(os.path.join(tmp, "leads.db"))
            store.record_search_run(
                city="Екатеринбург",
                preset="Стоматологии",
                osm_found=30,
                yandex_checked=12,
                sites_found=5,
                ready_leads=7,
                api_requests=12,
                estimated_cost=5.856,
            )

            history = store.list_search_runs()
            monthly_requests = store.monthly_yandex_requests()

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["osm_found"], 30)
        self.assertEqual(monthly_requests, 12)

    def test_upsert_preserves_manual_status_and_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LeadStore(os.path.join(tmp, "leads.db"))
            original = Lead(
                name="Мастер окон",
                lead_key="osm:node:1",
                phone="+7 343",
                score=80,
                reasons=["нет сайта", "есть телефон"],
                audit=WebsiteAudit(state="missing"),
            )
            store.upsert_many([original])
            store.update_status(original.lead_key, "Связался")
            store.update_note(original.lead_key, "Перезвонить в пятницу")

            refreshed = Lead(
                name="Мастер окон",
                lead_key="osm:node:1",
                phone="+7 999",
                score=90,
                reasons=["нет сайта", "есть телефон", "есть email"],
                email="mail@example.ru",
                audit=WebsiteAudit(state="missing"),
            )
            store.upsert_many([refreshed])
            saved = store.list_leads()[0]

        self.assertEqual(saved.phone, "+7 999")
        self.assertEqual(saved.score, 90)
        self.assertEqual(saved.status, "Связался")
        self.assertEqual(saved.note, "Перезвонить в пятницу")
        self.assertEqual(saved.audit, WebsiteAudit(state="missing"))

    def test_upsert_deduplicates_by_lead_key_and_sorts_by_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LeadStore(os.path.join(tmp, "leads.db"))
            store.upsert_many(
                [
                    Lead(name="Низкий", lead_key="a", score=10),
                    Lead(name="Высокий", lead_key="b", score=80),
                    Lead(name="Низкий обновлён", lead_key="a", score=20),
                ]
            )
            leads = store.list_leads()

        self.assertEqual([lead.lead_key for lead in leads], ["b", "a"])
        self.assertEqual(leads[1].name, "Низкий обновлён")

    def test_rejects_unknown_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LeadStore(os.path.join(tmp, "leads.db"))
            store.upsert_many([Lead(name="Компания", lead_key="a")])

            with self.assertRaises(ValueError):
                store.update_status("a", "Удалён")


if __name__ == "__main__":
    unittest.main()
