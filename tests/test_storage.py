import os
import tempfile
import unittest

from lead_finder import Lead, WebsiteAudit
from storage import LeadStore


class LeadStoreTests(unittest.TestCase):
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
