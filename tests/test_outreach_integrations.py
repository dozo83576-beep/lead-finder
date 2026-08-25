import imaplib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from unittest.mock import patch

from lead_finder import Lead, WebsiteAudit
from outreach import OutreachConfig, OutreachStore, ProviderSendResult
from outreach_integrations import (
    IMAP_TIMEOUT_SECONDS,
    MAX_BODY_CHARS,
    MAX_MESSAGE_BYTES,
    ImapReplyClient,
    IntegrationError,
    TelegramBotClient,
    UnisenderProvider,
    _message_text,
    _open_imap,
    parse_incoming_email,
)
from outreach_worker import OutreachWorker
from storage import LeadStore


UTC = timezone.utc


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.content = payload if isinstance(payload, bytes) else b""

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses, downloads=None):
        self.responses = list(responses)
        self.downloads = list(downloads or [])
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.responses.pop(0))

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.downloads.pop(0))


class IntegrationTests(unittest.TestCase):
    def make_store(self, path):
        lead = Lead(
            name="Компания",
            lead_key="lead-1",
            email="owner@example.ru",
            verification_status="confirmed_no_site",
            audit=WebsiteAudit(state="missing"),
        )
        LeadStore(path).upsert_many([lead])
        return OutreachStore(path), lead

    def enable(self, store, lead):
        store.upsert_permission(
            lead.lead_key,
            "email",
            lead.email,
            "consented",
            source="форма",
            evidence="opt-in",
            obtained_at=datetime.now(UTC).isoformat(),
        )
        for key in ("dns_verified", "unsubscribe_verified", "seed_delivery_verified", "production_enabled"):
            store.set_setting(key, True)

    def test_unisender_uses_bulk_campaign_api_and_consent_gate(self):
        config = OutreachConfig(
            unisender_api_key="secret-key",
            unisender_list_id="77",
            sender_name="Иван",
            sender_email="ivan@connect.example.ru",
            reply_to="ivan@connect.example.ru",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            blocked_session = FakeSession([])
            blocked = UnisenderProvider(config, session=blocked_session)
            with self.assertRaises(PermissionError):
                blocked.send_message(store, lead.lead_key, lead.email, "Тема", "Текст")
            self.assertEqual(blocked_session.calls, [])

            self.enable(store, lead)
            session = FakeSession(
                [
                    {"result": {}},
                    {"result": {"message_id": 10}},
                    {"result": {"campaign_id": 20}},
                ]
            )
            provider = UnisenderProvider(config, session=session)
            result = provider.send_message(
                store, lead.lead_key, lead.email, "Тема", "Текст", contact_name=lead.name
            )

        methods = [call[0].rsplit("/", 1)[-1] for call in session.calls]
        self.assertEqual(methods, ["subscribe", "createEmailMessage", "createCampaign"])
        self.assertEqual(session.calls[0][1]["data"]["double_optin"], 3)
        self.assertEqual(session.calls[0][1]["data"]["fields[Name]"], "Компания")
        self.assertEqual(session.calls[2][1]["data"]["track_read"], 0)
        self.assertEqual(session.calls[2][1]["data"]["track_links"], 0)
        self.assertEqual(result.provider_campaign_id, "20")

    def test_unisender_refuses_address_with_several_recipients(self):
        config = OutreachConfig(
            unisender_api_key="secret-key",
            unisender_list_id="77",
            sender_name="Иван",
            sender_email="ivan@connect.example.ru",
            reply_to="ivan@connect.example.ru",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            self.enable(store, lead)
            session = FakeSession([])
            provider = UnisenderProvider(config, session=session)

            with self.assertRaises(PermissionError):
                provider.send_message(
                    store, lead.lead_key, f"{lead.email}, other@example.ru", "Тема", "Текст"
                )

            self.assertEqual(session.calls, [])

    def test_parse_incoming_email_is_stable_without_message_id(self):
        message = EmailMessage()
        message["From"] = "Клиент <Owner@Example.ru>"
        message["Subject"] = "Ответ"
        message["Date"] = "Tue, 28 Jul 2026 10:00:00 +0500"
        message.set_content("Да, пришлите пример")
        first = parse_incoming_email(message)
        second = parse_incoming_email(message)
        self.assertEqual(first["address"], "owner@example.ru")
        self.assertEqual(first["provider_event_id"], second["provider_event_id"])
        self.assertIn("пришлите", first["body"])

    def test_imap_reply_matches_lead_and_stops_campaign(self):
        message = EmailMessage()
        message["From"] = "Клиент <owner@example.ru>"
        message["To"] = "ivan@connect.example.ru"
        message["Subject"] = "Ответ"
        message["Message-ID"] = "<reply-1@example.ru>"
        message.set_content("Да, интересно")

        class FakeImap:
            def __init__(self, host):
                self.seen = []

            def login(self, username, password):
                return "OK", []

            def select(self, mailbox):
                return "OK", []

            def search(self, charset, query):
                return "OK", [b"1"]

            def fetch(self, number, query):
                return "OK", [(b"1", message.as_bytes())]

            def store(self, number, operation, flag):
                self.seen.append(number)

            def logout(self):
                return "BYE", []

        config = OutreachConfig(
            sender_email="ivan@connect.example.ru",
            imap_host="imap.yandex.ru",
            imap_username="ivan@connect.example.ru",
            imap_password="app-password",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            self.enable(store, lead)
            campaign_id = store.create_campaign("IMAP", "no_site")
            store.set_campaign_state(campaign_id, "approved")
            store.set_campaign_state(campaign_id, "active")
            recipient_id = store.enroll_recipient(campaign_id, lead)
            self.assertEqual(ImapReplyClient(config, client_factory=FakeImap).sync(store), 1)
            with store._connect() as connection:
                recipient = connection.execute(
                    "SELECT * FROM outreach_recipients WHERE id = ?", (recipient_id,)
                ).fetchone()
            self.assertEqual(recipient["state"], "replied")
            self.assertEqual(store.list_events()[0]["event_type"], "reply")

    def test_imap_marks_unknown_sender_as_seen(self):
        message = EmailMessage()
        message["From"] = "Посторонний <stranger@example.com>"
        message["To"] = "ivan@connect.example.ru"
        message["Subject"] = "Реклама"
        message["Message-ID"] = "<spam-1@example.com>"
        message.set_content("Не относится к лидам")

        class FakeImap:
            def __init__(self, host):
                self.seen = []

            def login(self, username, password):
                return "OK", []

            def select(self, mailbox):
                return "OK", []

            def search(self, charset, query):
                return "OK", [b"1"]

            def fetch(self, number, query):
                return "OK", [(b"1", message.as_bytes())]

            def store(self, number, operation, flag):
                self.seen.append((number, flag))

            def logout(self):
                return "BYE", []

        config = OutreachConfig(
            sender_email="ivan@connect.example.ru",
            imap_host="imap.yandex.ru",
            imap_username="ivan@connect.example.ru",
            imap_password="app-password",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            self.enable(store, lead)
            captured = {}

            def factory(host):
                captured["client"] = FakeImap(host)
                return captured["client"]

            processed = ImapReplyClient(config, client_factory=factory).sync(store)

            # Событие не создаётся: отправитель не лид.
            self.assertEqual(processed, 0)
            self.assertEqual(store.list_events(), [])
            # Но письмо помечено прочитанным, иначе оно навсегда занимает место
            # в выборке UNSEEN и вытесняет из окна реальные ответы лидов.
            self.assertEqual(captured["client"].seen, [(b"1", "\Seen")])

    def test_imap_does_not_let_broken_messages_clog_the_window(self):
        class FakeImap:
            def __init__(self, host):
                self.seen = []
                self.fetched = []

            def login(self, username, password):
                return "OK", []

            def select(self, mailbox):
                return "OK", []

            def search(self, charset, query):
                return "OK", [b"1 2 3"]

            def fetch(self, number, query):
                self.fetched.append(number)
                if number == b"1":
                    return "NO", []          # временный сбой выборки
                if number == b"2":
                    return "OK", [(b"2", b"")]  # пустое тело
                return "OK", [(b"3", bytes([0xff, 0xfe]) + b" not a message")]

            def store(self, number, operation, flag):
                self.seen.append(number)

            def logout(self):
                return "BYE", []

        config = OutreachConfig(
            sender_email="ivan@connect.example.ru",
            imap_host="imap.yandex.ru",
            imap_username="ivan@connect.example.ru",
            imap_password="app-password",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            self.enable(store, lead)
            captured = {}

            def factory(host):
                captured["client"] = FakeImap(host)
                return captured["client"]

            with self.assertLogs("lead_finder.outreach", level="WARNING") as logs:
                processed = ImapReplyClient(config, client_factory=factory).sync(store)
            client = captured["client"]
            # Сбой выборки и пустое тело дают предупреждение каждый.
            self.assertEqual(len(logs.output), 2)

            self.assertEqual(processed, 0)
            self.assertEqual(client.fetched, [b"1", b"2", b"3"])
            # Письмо с временным сбоем выборки остаётся непрочитанным: оно вернётся
            # в следующем цикле. Пустое и то, из которого не вышло вытащить отправителя,
            # помечаются прочитанными — иначе они навсегда занимают места в окне UNSEEN.
            self.assertNotIn(b"1", client.seen)
            self.assertIn(b"2", client.seen)
            self.assertIn(b"3", client.seen)

    def test_imap_survives_message_with_unknown_charset(self):
        # Кодировки с таким именем в системе нет: bytes.decode бросает LookupError,
        # а он не подкласс ValueError — раньше это уронило бы весь цикл worker.
        broken = EmailMessage()
        broken["From"] = "Клиент <owner@example.ru>"
        broken["To"] = "ivan@connect.example.ru"
        broken["Subject"] = "Ответ"
        broken["Message-ID"] = "<broken-1@example.ru>"
        broken.set_content("Тело письма")
        broken.replace_header("Content-Type", 'text/plain; charset="nosuchcharset-42"')

        class FakeImap:
            def __init__(self, host):
                self.seen = []

            def login(self, username, password):
                return "OK", []

            def select(self, mailbox):
                return "OK", []

            def search(self, charset, query):
                return "OK", [b"7"]

            def fetch(self, number, query):
                return "OK", [(b"7", broken.as_bytes())]

            def store(self, number, operation, flag):
                self.seen.append(number)

            def logout(self):
                return "BYE", []

        config = OutreachConfig(
            sender_email="ivan@connect.example.ru",
            imap_host="imap.yandex.ru",
            imap_username="ivan@connect.example.ru",
            imap_password="app-password",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            self.enable(store, lead)
            captured = {}

            def factory(host):
                captured["client"] = FakeImap(host)
                return captured["client"]

            # Главное: sync не падает на таком письме, а пишет предупреждение.
            with self.assertLogs("lead_finder.outreach", level="WARNING") as logs:
                processed = ImapReplyClient(config, client_factory=factory).sync(store)
            self.assertIn("вручную", logs.output[0])

            # Письмо не зависает в окне UNSEEN, каким бы ни был исход разбора.
            self.assertIn(b"7", captured["client"].seen)
            self.assertIsInstance(processed, int)

    def test_imap_skips_oversized_message_and_caps_body(self):
        huge = EmailMessage()
        huge["From"] = "Клиент <owner@example.ru>"
        huge["To"] = "ivan@connect.example.ru"
        huge["Subject"] = "Большое письмо"
        huge["Message-ID"] = "<huge-1@example.ru>"
        huge.set_content("я" * (MAX_MESSAGE_BYTES // 2))

        class FakeImap:
            def __init__(self, host):
                self.seen = []

            def login(self, username, password):
                return "OK", []

            def select(self, mailbox):
                return "OK", []

            def search(self, charset, query):
                return "OK", [b"9"]

            def fetch(self, number, query):
                return "OK", [(b"9", huge.as_bytes())]

            def store(self, number, operation, flag):
                self.seen.append(number)

            def logout(self):
                return "BYE", []

        config = OutreachConfig(
            sender_email="ivan@connect.example.ru",
            imap_host="imap.yandex.ru",
            imap_username="ivan@connect.example.ru",
            imap_password="app-password",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            self.enable(store, lead)
            captured = {}

            def factory(host):
                captured["client"] = FakeImap(host)
                return captured["client"]

            with self.assertLogs("lead_finder.outreach", level="WARNING") as logs:
                processed = ImapReplyClient(config, client_factory=factory).sync(store)

            # Тело обрезано, но факт ответа лида не потерян: событие reply записано,
            # письмо не зависает в окне UNSEEN.
            self.assertIn("обрезано", logs.output[0])
            self.assertIn(b"9", captured["client"].seen)
            self.assertEqual(processed, 1)
            self.assertEqual(store.list_events()[0]["event_type"], "reply")

        # Тело обрезается до разбора, а не после: декодировать мегабайты незачем.
        self.assertEqual(len(_message_text(huge)), MAX_BODY_CHARS)

    def test_imap_network_failures_become_integration_errors(self):
        config = OutreachConfig(
            sender_email="ivan@connect.example.ru",
            imap_host="imap.yandex.ru",
            imap_username="ivan@connect.example.ru",
            imap_password="app-password",
        )

        def timing_out_factory(host):
            raise TimeoutError("timed out")

        class DroppingImap:
            def __init__(self, host):
                pass

            def login(self, username, password):
                raise ConnectionResetError("connection reset")

            def logout(self):
                return "BYE", []

        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            self.enable(store, lead)
            # Таймаут при подключении и обрыв в середине сессии — временные помехи:
            # worker в режиме --loop переживает IntegrationError, а необработанный
            # socket-таймаут уронил бы его.
            with self.assertRaises(IntegrationError):
                ImapReplyClient(config, client_factory=timing_out_factory).sync(store)
            with self.assertRaises(IntegrationError):
                ImapReplyClient(config, client_factory=DroppingImap).sync(store)

    def test_imap_connection_uses_timeout(self):
        captured = {}

        class FakeSSL:
            def __init__(self, host, timeout=None):
                captured["host"] = host
                captured["timeout"] = timeout

        with patch.object(imaplib, "IMAP4_SSL", FakeSSL):
            _open_imap("imap.yandex.ru")

        # Без таймаута зависший сервер молча останавливает worker в режиме --loop.
        self.assertEqual(captured["host"], "imap.yandex.ru")
        self.assertEqual(captured["timeout"], IMAP_TIMEOUT_SECONDS)

    def test_seed_test_uses_send_test_email_only_for_sender(self):
        config = OutreachConfig(
            unisender_api_key="secret-key",
            unisender_list_id="77",
            sender_name="Иван",
            sender_email="ivan@connect.example.ru",
            reply_to="ivan@connect.example.ru",
        )
        session = FakeSession(
            [
                {"result": {"message_id": 10}},
                {"message": "sent"},
            ]
        )
        message_id = UnisenderProvider(config, session=session).send_test_message("Тема", "Текст")
        self.assertEqual(message_id, "10")
        self.assertTrue(session.calls[1][0].endswith("/sendTestEmail"))
        self.assertEqual(session.calls[1][1]["data"]["email"], config.sender_email)

    def test_telegram_requires_start_and_processes_update_once(self):
        config = OutreachConfig(
            telegram_bot_token="token",
            telegram_bot_username="safe_bot",
            link_secret="link-secret",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            link = store.create_telegram_link(lead.lead_key, "safe_bot", "link-secret")
            token = link.split("start=", 1)[1]
            session = FakeSession(
                [
                    {"ok": True, "result": [{"update_id": 7, "message": {"chat": {"id": 123}, "text": f"/start {token}"}}]},
                    {"ok": True, "result": {"message_id": 1}},
                    {"ok": True, "result": [{"update_id": 7, "message": {"chat": {"id": 123}, "text": f"/start {token}"}}]},
                ]
            )
            client = TelegramBotClient(config, session=session)
            self.assertEqual(client.sync(store), 1)
            self.assertTrue(store.can_contact(lead.lead_key, "telegram", "tg:123"))
            self.assertEqual(client.sync(store), 0)
            self.assertEqual(len(store.list_telegram_messages()), 1)

    def test_telegram_reply_is_blocked_before_start(self):
        config = OutreachConfig(
            telegram_bot_token="token",
            telegram_bot_username="safe_bot",
            link_secret="link-secret",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self.make_store(os.path.join(tmp, "leads.db"))
            session = FakeSession([])
            with self.assertRaises(PermissionError):
                TelegramBotClient(config, session=session).reply(store, "123", "Здравствуйте")
            self.assertEqual(session.calls, [])

    def test_telegram_block_update_creates_suppression(self):
        config = OutreachConfig(
            telegram_bot_token="token",
            telegram_bot_username="safe_bot",
            link_secret="link-secret",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            link = store.create_telegram_link(lead.lead_key, "safe_bot", "link-secret")
            store.consume_telegram_link(link.split("start=", 1)[1], "123", "link-secret")
            session = FakeSession(
                [
                    {
                        "ok": True,
                        "result": [
                            {
                                "update_id": 9,
                                "my_chat_member": {
                                    "chat": {"id": 123},
                                    "new_chat_member": {"status": "kicked"},
                                },
                            }
                        ],
                    }
                ]
            )
            self.assertEqual(TelegramBotClient(config, session=session).sync(store), 1)
            self.assertFalse(store.can_contact(lead.lead_key, "telegram", "tg:123"))
            self.assertTrue(store.is_suppressed("telegram", "tg:123"))

    def test_unisender_async_report_creates_hard_bounce(self):
        config = OutreachConfig(
            unisender_api_key="secret-key",
            unisender_list_id="77",
            sender_name="Иван",
            sender_email="ivan@connect.example.ru",
            reply_to="ivan@connect.example.ru",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            self.enable(store, lead)
            campaign_id = store.create_campaign("Пилот", "no_site")
            store.set_campaign_state(campaign_id, "approved")
            store.set_campaign_state(campaign_id, "active")
            recipient_id = store.enroll_recipient(campaign_id, lead)
            with store._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO outreach_messages (
                        campaign_id, recipient_id, lead_key, channel, step_index, subject, body,
                        status, provider_message_id, provider_campaign_id, idempotency_key, sent_at
                    ) VALUES (?, ?, ?, 'email', 0, 'Тема', 'Текст', 'sent', '10', '20', 'test:async', ?)
                    """,
                    (campaign_id, recipient_id, lead.lead_key, datetime.now(UTC).isoformat()),
                )
                message = dict(connection.execute("SELECT * FROM outreach_messages").fetchone())
            session = FakeSession(
                [
                    {"result": {"sent": 1, "delivered": 0}},
                    {"result": {"task_uuid": "task-1", "status": "new"}},
                    {"result": {"sent": 1, "delivered": 0}},
                    {"result": {"task_uuid": "task-1", "status": "completed", "file_to_download": "https://files.example/report.csv"}},
                ],
                downloads=[b"Email,Result\nowner@example.ru,err_dest_invalid\n"],
            )
            provider = UnisenderProvider(config, session=session)
            self.assertEqual(provider.sync_message(store, message), 0)
            self.assertEqual(provider.sync_message(store, message), 1)

            refreshed = store.list_messages()[0]
            self.assertEqual(refreshed["status"], "bounced")
            self.assertTrue(store.is_suppressed("email", lead.email))

    def test_full_consent_email_reply_and_telegram_operator_cycle(self):
        class Provider:
            def send_message(self, store, lead_key, address, subject, body, contact_name=""):
                if not store.can_contact(lead_key, "email", address):
                    raise PermissionError("нет согласия")
                return ProviderSendResult("message-1", "campaign-1")

        email_config = OutreachConfig(
            unisender_api_key="test",
            unisender_list_id="1",
            sender_name="Иван",
            sender_email="ivan@connect.example.ru",
            reply_to="ivan@connect.example.ru",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store, lead = self.make_store(os.path.join(tmp, "leads.db"))
            self.enable(store, lead)
            campaign_id = store.create_campaign("Полный цикл", "no_site")
            store.set_campaign_state(campaign_id, "approved")
            store.set_campaign_state(campaign_id, "active")
            recipient_id = store.enroll_recipient(campaign_id, lead)
            now = datetime(2026, 7, 28, 5, 30, tzinfo=UTC)
            with store._connect() as connection:
                connection.execute(
                    "UPDATE outreach_recipients SET next_send_at = ? WHERE id = ?",
                    (now.isoformat(), recipient_id),
                )
            result = OutreachWorker(store, email_config, provider=Provider()).run_once(
                now=now, sync=False
            )
            self.assertEqual(result.sent, 1)
            message = store.list_messages()[0]
            store.record_event(
                "delivered", "delivery:1", "email", address=lead.email,
                lead_key=lead.lead_key, message_id=int(message["id"])
            )
            store.record_event(
                "reply", "imap:reply:1", "email", address=lead.email,
                lead_key=lead.lead_key, payload={"subject": "Да", "body": "Пришлите демо"}
            )
            with store._connect() as connection:
                recipient = connection.execute(
                    "SELECT * FROM outreach_recipients WHERE id = ?", (recipient_id,)
                ).fetchone()
            self.assertEqual(recipient["state"], "replied")
            self.assertIsNone(recipient["next_send_at"])

            telegram_config = OutreachConfig(
                telegram_bot_token="token",
                telegram_bot_username="safe_bot",
                link_secret="link-secret",
            )
            link = store.create_telegram_link(lead.lead_key, "safe_bot", "link-secret")
            token = link.split("start=", 1)[1]
            self.assertEqual(store.consume_telegram_link(token, "123", "link-secret"), lead.lead_key)
            session = FakeSession([{"ok": True, "result": {"message_id": 8}}])
            TelegramBotClient(telegram_config, session=session).reply(store, "123", "Вот ссылка на демо")
            messages = store.list_telegram_messages()
            self.assertEqual(messages[0]["direction"], "outbound")
            self.assertEqual(messages[0]["text"], "Вот ссылка на демо")


if __name__ == "__main__":
    unittest.main()
