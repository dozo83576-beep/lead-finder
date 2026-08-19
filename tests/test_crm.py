import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from crm import CRMStore
from lead_finder import Lead
from outreach import OutreachStore, ensure_outreach_schema
from storage import LeadStore


UTC = timezone.utc


class CRMTests(unittest.TestCase):
    def make_store(self, path: str, key: str = "lead-1") -> tuple[CRMStore, Lead]:
        lead = Lead(name="Тестовая компания", lead_key=key, email="owner@example.ru")
        LeadStore(path).upsert_many([lead])
        return CRMStore(path), lead

    def advance_to_negotiation(self, store: CRMStore, deal_id: int) -> None:
        for stage in ("qualified", "discovery", "proposal", "negotiation"):
            store.transition_deal(deal_id, stage)

    def test_migration_is_additive_and_does_not_create_deals_for_existing_leads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leads.db")
            base = LeadStore(path)
            base.upsert_many([Lead(name="Старый лид", lead_key="old", status="Связался", note="Не потерять")])
            before = base.list_leads()[0]

            CRMStore(path)

            after = LeadStore(path).list_leads()[0]
            connection = sqlite3.connect(path)
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            deal_count = connection.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
            connection.close()

        self.assertEqual((after.lead_key, after.status, after.note), (before.lead_key, before.status, before.note))
        self.assertEqual(deal_count, 0)
        self.assertTrue({"partners", "referrals", "deals", "deal_payments", "partner_payouts"} <= tables)

    def test_migration_preserves_preexisting_outreach_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leads.db")
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE leads (
                    lead_key TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
                    social TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'Новый',
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "INSERT INTO leads VALUES ('old', 'Старый лид', 'owner@example.ru', '', 'Связался', 'Важно')"
            )
            ensure_outreach_schema(connection)
            connection.execute(
                """
                INSERT INTO contact_permissions (lead_key, channel, address, status, source, evidence)
                VALUES ('old', 'email', 'owner@example.ru', 'consented', 'форма', 'opt-in 42')
                """
            )
            connection.execute(
                "INSERT INTO outreach_campaigns (name, segment) VALUES ('Старая кампания', 'no_site')"
            )
            connection.execute(
                "INSERT INTO outreach_suppressions (channel, address, reason) VALUES ('email', 'stop@example.ru', 'manual')"
            )
            connection.commit()
            connection.close()

            CRMStore(path)

            connection = sqlite3.connect(path)
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("contact_permissions", "outreach_campaigns", "outreach_suppressions")
            }
            lead_state = connection.execute(
                "SELECT status, note FROM leads WHERE lead_key = 'old'"
            ).fetchone()
            connection.close()

        self.assertEqual(counts, {
            "contact_permissions": 1, "outreach_campaigns": 1, "outreach_suppressions": 1
        })
        self.assertEqual(lead_state, ("Связался", "Важно"))

    def test_approved_positioning_requires_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self.make_store(os.path.join(tmp, "leads.db"))
            profile_id = store.create_positioning_profile(
                "Telegram", "Владельцы клиник", "Проверка потерь заявок", "", "Обсудить аудит"
            )
            with self.assertRaisesRegex(ValueError, "доказательств"):
                store.approve_positioning_profile(profile_id)

            profile_with_proof = store.create_positioning_profile(
                "Telegram", "Владельцы клиник", "Проверка потерь заявок",
                "3 обезличенных кейса с договорами", "Обсудить аудит"
            )
            store.approve_positioning_profile(profile_with_proof)
            states = {row["id"]: row["state"] for row in store.list_positioning_profiles()}

        self.assertEqual(states[profile_id], "draft")
        self.assertEqual(states[profile_with_proof], "approved")

    def test_networking_log_never_uses_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            source_id = store.create_source(
                "community", "Чат стоматологов", platform="telegram", url="https://t.me/example",
                niche="стоматологии", activity_score=4, audience_fit_score=5
            )
            with patch("socket.socket") as network:
                store.add_networking_activity(
                    "public_comment", "Ответил на вопрос о мобильной версии",
                    acquisition_source_id=source_id, lead_key=lead.lead_key,
                    reference_url="https://t.me/example/42", outcome="Получен уточняющий вопрос",
                    next_task="Ответить вручную"
                )
            network.assert_not_called()
            activity = store.list_networking_activities()[0]

        self.assertEqual(activity["action_type"], "public_comment")
        self.assertEqual(activity["source_name"], "Чат стоматологов")

    def test_referral_is_idempotent_and_does_not_grant_consent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leads.db")
            store, lead = self.make_store(path)
            partner_id = store.create_partner("Анна", "Встреча на конференции")
            first = store.create_referral(
                partner_id, lead.lead_key, "личное знакомство", "Письмо Анны с представлением",
                idempotency_key="intro-42"
            )
            second = store.create_referral(
                partner_id, lead.lead_key, "личное знакомство", "Письмо Анны с представлением",
                idempotency_key="intro-42"
            )

            permission = OutreachStore(path).get_permission(lead.lead_key, "email", lead.email)
            referrals = store.list_referrals()
            deals = store.list_deals()

        self.assertEqual(first, second)
        self.assertEqual(len(referrals), 1)
        self.assertEqual(len(deals), 1)
        self.assertEqual(permission["status"], "unknown")

    def test_partner_or_crm_cannot_bypass_suppression(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leads.db")
            store, lead = self.make_store(path)
            outreach = OutreachStore(path)
            outreach.upsert_permission(
                lead.lead_key, "email", lead.email, "consented", source="форма",
                evidence="double opt-in 42", obtained_at="2026-07-28T10:00:00+00:00"
            )
            outreach.add_suppression("email", lead.email, "manual", "operator")
            partner_id = store.create_partner("Анна", "Личная встреча")
            store.create_referral(
                partner_id, lead.lead_key, "email", "Общее письмо", idempotency_key="suppressed-ref"
            )

            permission = outreach.get_permission(lead.lead_key, "email", lead.email)
            can_contact = outreach.can_contact(lead.lead_key, "email", lead.email)

        self.assertEqual(permission["status"], "consented")
        self.assertFalse(can_contact)

    def test_inbox_deal_creation_is_idempotent_and_has_qualification_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            first = store.create_deal_from_inbox("email", "event-7", lead.lead_key)
            second = store.create_deal_from_inbox("email", "event-7", lead.lead_key)
            deals = store.list_deals()
            tasks = store.list_tasks(first)

        self.assertEqual(first, second)
        self.assertEqual(len(deals), 1)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_type"], "qualify")

    def test_invalid_transitions_and_terminal_requirements_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            deal_id = store.create_deal(lead.lead_key, "Новый сайт")
            with self.assertRaisesRegex(ValueError, "недопустим"):
                store.transition_deal(deal_id, "proposal")
            with self.assertRaisesRegex(ValueError, "причину"):
                store.transition_deal(deal_id, "lost")

            self.advance_to_negotiation(store, deal_id)
            with self.assertRaisesRegex(ValueError, "стоимость"):
                store.transition_deal(deal_id, "won")
            store.transition_deal(deal_id, "won", value_kopecks=250_000_00)
            deal = store.list_deals()[0]
            history = store.list_stage_history(deal_id)

        self.assertEqual(deal["stage"], "won")
        self.assertEqual(deal["value_kopecks"], 250_000_00)
        self.assertEqual([row["to_stage"] for row in history],
                         ["new", "qualified", "discovery", "proposal", "negotiation", "won"])

    def test_one_open_deal_per_lead_and_subject_but_new_after_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            first = store.create_deal(lead.lead_key, "  Новый сайт ")
            same = store.create_deal(lead.lead_key, "новый   сайт")
            store.transition_deal(first, "lost", reason="Клиент отложил проект")
            new = store.create_deal(lead.lead_key, "Новый сайт")
            deals = store.list_deals()

        self.assertEqual(first, same)
        self.assertNotEqual(first, new)
        self.assertEqual(len(deals), 2)

    def test_partial_and_cancelled_payment_keep_commission_accrued(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leads.db")
            store, lead = self.make_store(path)
            partner_id = store.create_partner(
                "Анна", "Договор о партнёрстве", default_commission_bps=1250, payout_delay_days=3
            )
            _, deal_id = store.create_referral(
                partner_id, lead.lead_key, "email", "Представление в общем письме", idempotency_key="ref-1"
            )
            self.assertIsNone(store.reconcile_payout(deal_id))
            self.assertEqual(store.list_payouts(), [])
            self.advance_to_negotiation(store, deal_id)
            store.transition_deal(deal_id, "won", value_kopecks=100_000_00)
            payment_id = store.add_payment(
                deal_id, 40_000_00, status="paid", paid_at="2026-07-28T10:00:00+00:00"
            )

            payout = store.reconcile_payout(deal_id, "2026-08-10T10:00:00+00:00")
            store.set_payment_status(payment_id, "cancelled")
            cancelled = store.reconcile_payout(deal_id, "2026-08-10T10:00:00+00:00")

        self.assertEqual(payout["status"], "accrued")
        self.assertEqual(payout["amount_kopecks"], 12_500_00)
        self.assertEqual(cancelled["status"], "accrued")
        self.assertIsNone(cancelled["due_at"])

    def test_full_manual_networking_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leads.db")
            store, lead = self.make_store(path)
            source_id = store.create_source(
                "community", "Чат клиник", platform="telegram", niche="клиники",
                state="engaging", activity_score=4, audience_fit_score=5
            )
            store.add_networking_activity(
                "public_comment", "Полезный ответ о форме записи", acquisition_source_id=source_id,
                lead_key=lead.lead_key, outcome="Владелец задал вопрос"
            )
            store.add_networking_activity(
                "inbound_reply", "Владелец попросил обсудить аудит", acquisition_source_id=source_id,
                lead_key=lead.lead_key, next_task="Квалифицировать"
            )
            OutreachStore(path).upsert_permission(
                lead.lead_key, "email", lead.email, "inbound", source="ответ в сообществе",
                evidence="ссылка на сообщение 42", obtained_at="2026-07-28T10:00:00+00:00"
            )
            store.add_lead_to_crm(
                lead.lead_key, "community", acquisition_source_id=source_id,
                next_step="Провести квалификацию"
            )
            deal_id = store.create_deal_from_inbox("community", "message-42", lead.lead_key, "Новый сайт")
            self.advance_to_negotiation(store, deal_id)
            store.transition_deal(deal_id, "won", value_kopecks=80_000_00)
            store.add_payment(deal_id, 80_000_00, status="paid")

            summary = store.lead_summary(lead.lead_key)
            metrics = store.metrics()

        self.assertEqual(summary["profile"]["source_kind"], "community")
        self.assertEqual(len(summary["activities"]), 2)
        self.assertEqual(metrics["active_sources"], 1)
        self.assertEqual(metrics["inbound_replies"], 1)
        self.assertEqual(metrics["won_kopecks"], 80_000_00)

    def test_full_partner_scenario_calculates_integer_commission_and_manual_payment(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            partner_id = store.create_partner(
                "Анна", "Подписанное партнёрское соглашение", default_commission_bps=1000,
                payout_delay_days=3, state="active"
            )
            _, deal_id = store.create_referral(
                partner_id, lead.lead_key, "встреча", "Клиент представлен на встрече",
                idempotency_key="partner-flow"
            )
            self.advance_to_negotiation(store, deal_id)
            store.transition_deal(deal_id, "won", value_kopecks=123_456_78)
            paid_at = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
            store.add_payment(deal_id, 23_456_78, status="paid", paid_at=paid_at.isoformat())
            first = store.reconcile_payout(deal_id, paid_at + timedelta(days=10))
            self.assertEqual(first["status"], "accrued")
            store.add_payment(deal_id, 100_000_00, status="paid", paid_at=paid_at.isoformat())
            due = store.reconcile_payout(deal_id, paid_at + timedelta(days=3))
            store.mark_payout_paid(int(due["id"]), (paid_at + timedelta(days=4)).isoformat())
            payout = store.list_payouts()[0]
            metrics = store.metrics()

        self.assertEqual(due["basis_kopecks"], 123_456_78)
        self.assertEqual(due["amount_kopecks"], 12_345_67)
        self.assertEqual(payout["status"], "paid")
        self.assertEqual(metrics["commission_paid_kopecks"], 12_345_67)

    def test_verified_observation_requires_evidence_and_finance_export_omits_contacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            deal_id = store.create_deal(lead.lead_key, "Аудит")
            with self.assertRaisesRegex(ValueError, "доказательство"):
                store.add_deal_note(deal_id, "verified_observation", "Нет мобильного viewport")
            store.add_deal_note(
                deal_id, "verified_observation", "Нет мобильного viewport", "HTML-аудит 2026-07-28"
            )
            export = store.financial_export_csv().decode("utf-8-sig")
            notes = store.list_deal_notes(deal_id)

        self.assertNotIn(lead.email, export)
        self.assertNotIn("Нет мобильного viewport", export)
        self.assertEqual(notes[0]["evidence"], "HTML-аудит 2026-07-28")


    def test_financial_export_neutralizes_formula_injection_from_lead_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leads.db")
            hostile = Lead(name="=HYPERLINK(\"http://evil\",\"счёт\")", lead_key="hostile")
            LeadStore(path).upsert_many([hostile])
            store = CRMStore(path)
            deal_id = store.create_deal(hostile.lead_key, "@SUM(1+1)")
            self.advance_to_negotiation(store, deal_id)
            store.transition_deal(deal_id, "won", value_kopecks=100_000_00)
            store.add_payment(deal_id, 100_000_00, status="paid")

            export = store.financial_export_csv().decode("utf-8-sig")

        self.assertIn("'=HYPERLINK", export)
        self.assertIn("'@SUM(1+1)", export)
        self.assertIn("10000000", export)


if __name__ == "__main__":
    unittest.main()
