import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from lead_finder import Lead, WebsiteAudit
from outreach import OutreachConfig, OutreachStore, ProviderSendResult, render_sequence, segment_for_lead
from outreach_worker import OutreachWorker, retry_delay
from storage import LeadStore


UTC = timezone.utc


def confirmed_lead(key: str = "lead-1", email: str = "owner@example.ru") -> Lead:
    return Lead(
        name="Тестовая компания",
        lead_key=key,
        category="ремонт",
        email=email,
        verification_status="confirmed_no_site",
        audit=WebsiteAudit(state="missing"),
    )


class FakeProvider:
    def __init__(self):
        self.calls = 0

    def send_message(self, store, lead_key, address, subject, body, contact_name=""):
        if not store.can_contact(lead_key, "email", address):
            raise PermissionError("нет согласия")
        ready, _ = store.production_gate_ready()
        if not ready:
            raise PermissionError("production gate")
        self.calls += 1
        return ProviderSendResult(f"message-{self.calls}", f"campaign-{self.calls}")


class OutreachTests(unittest.TestCase):
    def make_store(self, path: str, lead: Lead | None = None) -> tuple[LeadStore, OutreachStore, Lead]:
        base = LeadStore(path)
        selected = lead or confirmed_lead()
        base.upsert_many([selected])
        return base, OutreachStore(path), selected

    def grant(self, store: OutreachStore, lead: Lead) -> None:
        store.upsert_permission(
            lead.lead_key,
            "email",
            lead.email,
            "consented",
            source="форма на сайте",
            evidence="запись opt-in №42",
            obtained_at=datetime.now(UTC).isoformat(),
        )

    def open_campaign(self, store: OutreachStore, lead: Lead, now: datetime) -> int:
        campaign_id = store.create_campaign("Пилот", "no_site", daily_limit=5)
        store.set_campaign_state(campaign_id, "approved")
        store.set_campaign_state(campaign_id, "active")
        recipient_id = store.enroll_recipient(campaign_id, lead, now)
        with store._connect() as connection:
            connection.execute(
                "UPDATE outreach_recipients SET next_send_at = ? WHERE id = ?",
                ((now - timedelta(seconds=1)).isoformat(), recipient_id),
            )
        return campaign_id

    def enable_production(self, store: OutreachStore) -> None:
        for key in ("dns_verified", "unsubscribe_verified", "seed_delivery_verified", "production_enabled"):
            store.set_setting(key, True)

    def test_migration_preserves_existing_lead_and_adds_outreach_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leads.db")
            base, _, lead = self.make_store(path)
            before = base.list_leads()[0]
            connection = sqlite3.connect(path)
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            connection.close()

        self.assertEqual(before.lead_key, lead.lead_key)
        self.assertIn("contact_permissions", tables)
        self.assertIn("outreach_campaigns", tables)
        self.assertIn("outreach_suppressions", tables)

    def test_unknown_and_withdrawn_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            campaign_id = store.create_campaign("Пилот", "no_site")
            self.assertEqual(
                store.get_permission(lead.lead_key, "email", lead.email)["status"],
                "unknown",
            )
            self.assertFalse(store.can_contact(lead.lead_key, "email", lead.email))
            with self.assertRaises(PermissionError):
                store.enroll_recipient(campaign_id, lead)
            self.grant(store, lead)
            self.assertTrue(store.can_contact(lead.lead_key, "email", lead.email))
            store.upsert_permission(lead.lead_key, "email", lead.email, "withdrawn")
            self.assertFalse(store.can_contact(lead.lead_key, "email", lead.email))
            self.assertTrue(store.is_suppressed("email", lead.email))

    def test_consent_requires_source_evidence_and_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            with self.assertRaises(ValueError):
                store.upsert_permission(lead.lead_key, "email", lead.email, "consented")

    def test_unconfirmed_no_site_cannot_be_rendered(self):
        lead = confirmed_lead()
        lead.verification_status = "likely_no_site"
        lead.audit = WebsiteAudit(state="missing")
        self.assertIsNone(segment_for_lead(lead))
        with self.assertRaises(ValueError):
            render_sequence(lead)

    def test_templates_use_only_confirmed_observation(self):
        no_site = confirmed_lead()
        first = render_sequence(no_site)[0]
        self.assertIn("подтверждено вручную", first.body)
        self.assertNotIn("у вас нет сайта", first.body.casefold())
        existing = confirmed_lead("lead-2")
        existing.website = "https://example.ru"
        existing.verification_status = "site_found"
        existing.audit = WebsiteAudit(
            state="reachable",
            normalized_url="https://example.ru",
            https=True,
            mobile_viewport=False,
        )
        self.assertIn("мобильная адаптация", render_sequence(existing)[0].body)

    def test_dry_run_has_no_external_calls_or_message_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            self.grant(store, lead)
            now = datetime(2026, 7, 28, 5, 30, tzinfo=UTC)
            self.open_campaign(store, lead, now)
            provider = FakeProvider()
            worker = OutreachWorker(store, OutreachConfig(), provider=provider)
            result = worker.run_once(dry_run=True, now=now)

            self.assertEqual(len(result.previews), 1)
            self.assertEqual(provider.calls, 0)
            self.assertEqual(store.list_messages(), [])

    def test_repeated_worker_does_not_send_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            self.grant(store, lead)
            self.enable_production(store)
            now = datetime(2026, 7, 28, 5, 30, tzinfo=UTC)
            self.open_campaign(store, lead, now)
            provider = FakeProvider()
            config = OutreachConfig(
                unisender_api_key="test",
                unisender_list_id="1",
                sender_name="Иван",
                sender_email="ivan@connect.example.ru",
                reply_to="ivan@connect.example.ru",
            )
            worker = OutreachWorker(store, config, provider=provider)
            first = worker.run_once(now=now, sync=False)
            second = worker.run_once(now=now, sync=False)

            self.assertEqual(first.sent, 1)
            self.assertEqual(second.sent, 0)
            self.assertEqual(provider.calls, 1)

    def test_stop_events_are_idempotent_and_stop_next_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            self.grant(store, lead)
            now = datetime(2026, 7, 28, 5, 30, tzinfo=UTC)
            campaign_id = self.open_campaign(store, lead, now)
            inserted = store.record_event(
                "unsubscribe",
                "provider:event:1",
                "email",
                address=lead.email,
                lead_key=lead.lead_key,
            )
            duplicate = store.record_event(
                "unsubscribe",
                "provider:event:1",
                "email",
                address=lead.email,
                lead_key=lead.lead_key,
            )
            with store._connect() as connection:
                recipient = connection.execute(
                    "SELECT * FROM outreach_recipients WHERE campaign_id = ?", (campaign_id,)
                ).fetchone()

            self.assertTrue(inserted)
            self.assertFalse(duplicate)
            self.assertEqual(recipient["state"], "suppressed")
            self.assertIsNone(recipient["next_send_at"])
            self.assertTrue(store.is_suppressed("email", lead.email))

    def test_production_gate_blocks_real_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            self.grant(store, lead)
            now = datetime(2026, 7, 28, 5, 30, tzinfo=UTC)
            self.open_campaign(store, lead, now)
            config = OutreachConfig(
                unisender_api_key="test",
                unisender_list_id="1",
                sender_name="Иван",
                sender_email="ivan@connect.example.ru",
                reply_to="ivan@connect.example.ru",
            )
            with self.assertRaises(PermissionError):
                OutreachWorker(store, config, provider=FakeProvider()).run_once(now=now, sync=False)

    def test_complaint_and_second_hard_bounce_pause_small_campaign(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leads.db")
            _, store, first_lead = self.make_store(path)
            second_lead = confirmed_lead("lead-2", "second@example.ru")
            LeadStore(path).upsert_many([second_lead])
            self.grant(store, first_lead)
            self.grant(store, second_lead)
            now = datetime(2026, 7, 28, 5, 30, tzinfo=UTC)
            campaign_id = self.open_campaign(store, first_lead, now)
            store.enroll_recipient(campaign_id, second_lead, now)
            sequence = render_sequence(first_lead)
            with store._connect() as connection:
                recipients = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT r.*, c.segment, c.timezone, c.daily_limit, c.state AS campaign_state
                        FROM outreach_recipients r
                        JOIN outreach_campaigns c ON c.id = r.campaign_id
                        WHERE r.campaign_id = ? ORDER BY r.id
                        """,
                        (campaign_id,),
                    ).fetchall()
                ]
            message_ids = []
            for recipient in recipients:
                rendered = sequence[0] if recipient["lead_key"] == first_lead.lead_key else render_sequence(second_lead)[0]
                claimed = store.claim_message(recipient, rendered)
                message_ids.append(int(claimed["id"]))
            store.record_event(
                "hard_bounce", "bounce:1", "email", address=first_lead.email,
                lead_key=first_lead.lead_key, message_id=message_ids[0]
            )
            self.assertEqual(store.get_campaign(campaign_id)["state"], "active")
            store.record_event(
                "hard_bounce", "bounce:2", "email", address=second_lead.email,
                lead_key=second_lead.lead_key, message_id=message_ids[1]
            )
            self.assertEqual(store.get_campaign(campaign_id)["state"], "paused")

            complaint_lead = confirmed_lead("lead-3", "complaint@example.ru")
            LeadStore(path).upsert_many([complaint_lead])
            self.grant(store, complaint_lead)
            complaint_id = store.create_campaign("Жалоба", "no_site")
            store.set_campaign_state(complaint_id, "approved")
            store.set_campaign_state(complaint_id, "active")
            recipient_id = store.enroll_recipient(complaint_id, complaint_lead, now)
            with store._connect() as connection:
                row = connection.execute("SELECT * FROM outreach_recipients WHERE id = ?", (recipient_id,)).fetchone()
            claimed = store.claim_message(dict(row), render_sequence(complaint_lead)[0])
            store.record_event(
                "complaint", "complaint:1", "email", address=complaint_lead.email,
                lead_key=complaint_lead.lead_key, message_id=int(claimed["id"])
            )
            self.assertEqual(store.get_campaign(complaint_id)["state"], "paused")

    def test_daily_limit_grows_by_at_most_25_percent_after_seven_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, _ = self.make_store(os.path.join(tmp, "leads.db"))
            campaign_id = store.create_campaign("Рост", "no_site", daily_limit=5)
            now = datetime(2026, 7, 28, 5, 30, tzinfo=UTC)
            with store._connect() as connection:
                connection.execute(
                    "UPDATE outreach_campaigns SET created_at = ? WHERE id = ?",
                    ((now - timedelta(days=8)).isoformat(), campaign_id),
                )
            with self.assertRaises(ValueError):
                store.increase_daily_limit(campaign_id, 7, now)
            store.increase_daily_limit(campaign_id, 6, now)
            self.assertEqual(store.get_campaign(campaign_id)["daily_limit"], 6)
            with self.assertRaises(ValueError):
                store.increase_daily_limit(campaign_id, 7, now + timedelta(days=1))

    def test_claim_refuses_slot_over_daily_limit_without_relying_on_prior_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leads.db")
            _, store, first_lead = self.make_store(path)
            now = datetime(2026, 7, 28, 5, 30, tzinfo=UTC)
            campaign_id = store.create_campaign("Лимит", "no_site", daily_limit=1)
            store.set_campaign_state(campaign_id, "approved")
            store.set_campaign_state(campaign_id, "active")
            self.grant(store, first_lead)
            second_lead = confirmed_lead("lead-2", "second@example.ru")
            LeadStore(path).upsert_many([second_lead])
            self.grant(store, second_lead)
            store.enroll_recipient(campaign_id, first_lead, now)
            store.enroll_recipient(campaign_id, second_lead, now)
            with store._connect() as connection:
                recipients = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT r.*, c.segment, c.timezone, c.daily_limit
                        FROM outreach_recipients r
                        JOIN outreach_campaigns c ON c.id = r.campaign_id
                        WHERE r.campaign_id = ? ORDER BY r.id
                        """,
                        (campaign_id,),
                    ).fetchall()
                ]

            first = store.claim_message_within_limit(
                recipients[0], render_sequence(first_lead)[0], now, 1
            )
            # Второй вызов имитирует параллельный процесс: он не видел mark_message_sent,
            # но слот уже занят письмом в статусе sending.
            second = store.claim_message_within_limit(
                recipients[1], render_sequence(second_lead)[0], now, 1
            )

            self.assertIsNotNone(first)
            self.assertIsNone(second)

    def test_pending_sync_keeps_old_sent_messages_in_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leads.db")
            _, store, lead = self.make_store(path)
            with store._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO outreach_messages (
                        lead_key, channel, step_index, subject, body, status,
                        provider_campaign_id, idempotency_key
                    ) VALUES (?, 'email', 0, 'Тема', 'Текст', 'sent', 'campaign-1', 'old-sent')
                    """,
                    (lead.lead_key,),
                )
                for index in range(150):
                    connection.execute(
                        """
                        INSERT INTO outreach_messages (
                            lead_key, channel, step_index, subject, body, status,
                            provider_campaign_id, idempotency_key
                        ) VALUES (?, 'email', 0, 'Тема', 'Текст', 'draft', '', ?)
                        """,
                        (lead.lead_key, f"draft-{index}"),
                    )

            pending = store.list_messages_pending_sync(100)
            recent = store.list_messages(100)

            self.assertEqual([row["idempotency_key"] for row in pending], ["old-sent"])
            self.assertNotIn("old-sent", [row["idempotency_key"] for row in recent])

    def test_claim_distinguishes_busy_database_from_structural_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            now = datetime(2026, 7, 28, 5, 30, tzinfo=UTC)
            recipient = {
                "campaign_id": 1,
                "id": 1,
                "lead_key": lead.lead_key,
                "timezone": "Asia/Yekaterinburg",
            }
            rendered = render_sequence(lead)[0]

            # Конкуренция за запись — штатная помеха, слот просто не выдан,
            # но след в журнале остаться обязан.
            busy = sqlite3.OperationalError("database is locked")
            with patch.object(store, "_claim_within_limit", side_effect=busy):
                with self.assertLogs("lead_finder.outreach", level="WARNING") as logs:
                    self.assertIsNone(store.claim_message_within_limit(recipient, rendered, now, 5))
            self.assertIn("занята", logs.output[0])

            # Структурная поломка обязана быть видимой, иначе worker молча
            # крутит холостые циклы и рассылка стоит без единой строки в логе.
            broken = sqlite3.OperationalError("no such column: last_sync_at")
            with patch.object(store, "_claim_within_limit", side_effect=broken):
                with self.assertRaises(sqlite3.OperationalError):
                    store.claim_message_within_limit(recipient, rendered, now, 5)

    def test_sync_window_rotates_and_does_not_starve_new_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leads.db")
            _, store, lead = self.make_store(path)
            with store._connect() as connection:
                for index in range(120):
                    connection.execute(
                        """
                        INSERT INTO outreach_messages (
                            lead_key, channel, step_index, subject, body, status,
                            provider_campaign_id, idempotency_key
                        ) VALUES (?, 'email', 0, 'Тема', 'Текст', 'delivered', 'campaign-1', ?)
                        """,
                        (lead.lead_key, f"stuck-{index:03d}"),
                    )

            # Первое окно берёт сотню самых давних и отмечает их проверенными.
            first_window = store.list_messages_pending_sync(100)
            for message in first_window:
                store.mark_message_synced(int(message["id"]))
            second_window = store.list_messages_pending_sync(100)

            first_keys = {row["idempotency_key"] for row in first_window}
            second_keys = {row["idempotency_key"] for row in second_window}
            self.assertEqual(len(first_window), 100)
            # Двадцать писем, не попавших в первое окно, обязаны попасть во второе:
            # иначе застрявшие в delivered навсегда блокируют проверку остальных.
            self.assertTrue({"stuck-100", "stuck-119"} <= second_keys)
            self.assertEqual(len(second_keys - first_keys), 20)

    def test_retry_delay_grows_while_provider_keeps_failing(self):
        # Успешный цикл — обычный интервал.
        self.assertEqual(retry_delay(60, 0), 60)
        # Повторяющийся отказ разводит попытки, чтобы не словить бан у провайдера.
        self.assertEqual(retry_delay(60, 1), 120)
        self.assertEqual(retry_delay(60, 2), 240)
        # Рост ограничен сверху и не уходит в бесконечность.
        self.assertEqual(retry_delay(60, 50), 1800)
        self.assertEqual(retry_delay(600, 3), 1800)
        # Интервал больше потолка: пауза после сбоя не может стать короче штатной.
        self.assertEqual(retry_delay(3600, 1), 3600)
        self.assertEqual(retry_delay(3600, 9), 3600)
        for interval in (10, 60, 600, 1800, 3600, 7200):
            for failures in range(0, 12):
                self.assertGreaterEqual(retry_delay(interval, failures), interval)

    def test_small_daily_limit_cannot_grow_and_says_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, _ = self.make_store(os.path.join(tmp, "leads.db"))
            now = datetime(2026, 7, 28, 5, 30, tzinfo=UTC)
            campaign_id = store.create_campaign("Малый лимит", "no_site", daily_limit=3)
            with store._connect() as connection:
                connection.execute(
                    "UPDATE outreach_campaigns SET created_at = ? WHERE id = ?",
                    ((now - timedelta(days=8)).isoformat(), campaign_id),
                )

            # 25% от 3 не дают целого адресата: рост невозможен, и отказ это объясняет.
            with self.assertRaises(ValueError) as error:
                store.increase_daily_limit(campaign_id, 4, now)
            self.assertIn("не даёт целого адресата", str(error.exception))
            self.assertEqual(store.get_campaign(campaign_id)["daily_limit"], 3)

    def test_worker_never_sends_outside_campaign_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            self.grant(store, lead)
            self.enable_production(store)
            outside_window = datetime(2026, 7, 28, 8, tzinfo=UTC)  # 13:00 в Екатеринбурге
            self.open_campaign(store, lead, outside_window)
            provider = FakeProvider()
            config = OutreachConfig(
                unisender_api_key="test",
                unisender_list_id="1",
                sender_name="Иван",
                sender_email="ivan@connect.example.ru",
                reply_to="ivan@connect.example.ru",
            )
            result = OutreachWorker(store, config, provider=provider).run_once(
                now=outside_window, sync=False
            )
            self.assertEqual(result.sent, 0)
            self.assertEqual(provider.calls, 0)


if __name__ == "__main__":
    unittest.main()
