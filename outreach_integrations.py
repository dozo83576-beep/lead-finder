from __future__ import annotations

import email
import hashlib
import imaplib
import json
import logging
import gzip
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

import requests

from outreach import (
    OutreachConfig,
    OutreachStore,
    ProviderSendResult,
    is_single_destination,
    normalize_destination,
    plain_text_to_html,
)


UTC = timezone.utc


LOGGER = logging.getLogger("lead_finder.outreach")
IMAP_TIMEOUT_SECONDS = 30
MAX_MESSAGE_BYTES = 1_000_000
MAX_BODY_CHARS = 10_000


class IntegrationError(RuntimeError):
    pass


class TelegramBlockedError(IntegrationError):
    pass


def _safe_error(prefix: str, response: requests.Response | None = None) -> IntegrationError:
    if response is None:
        return IntegrationError(prefix)
    status = getattr(response, "status_code", "unknown")
    return IntegrationError(f"{prefix}: HTTP {status}")


class UnisenderProvider:
    def __init__(
        self,
        config: OutreachConfig,
        session: requests.Session | Any | None = None,
        base_url: str = "https://api.unisender.com/ru/api",
    ):
        self.config = config
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")

    def validate(self) -> None:
        if not self.config.email_ready:
            raise IntegrationError("Unisender не настроен полностью")
        if normalize_destination("email", self.config.reply_to) != normalize_destination(
            "email", self.config.sender_email
        ):
            raise IntegrationError(
                "Для campaign API Unisender OUTREACH_REPLY_TO должен совпадать с OUTREACH_FROM_EMAIL"
            )

    def _call(self, method: str, data: dict[str, object]) -> dict[str, object]:
        self.validate()
        payload = {"format": "json", "api_key": self.config.unisender_api_key, **data}
        try:
            response = self.session.post(
                f"{self.base_url}/{method}",
                data=payload,
                timeout=30,
            )
            response.raise_for_status()
            result = response.json()
        except requests.RequestException as error:
            response_value = getattr(error, "response", None)
            raise _safe_error(f"Ошибка Unisender ({method})", response_value) from error
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise IntegrationError(f"Unisender вернул некорректный ответ ({method})") from error
        if not isinstance(result, dict):
            raise IntegrationError(f"Unisender вернул некорректный ответ ({method})")
        if result.get("error"):
            code = str(result.get("code") or "api_error")
            raise IntegrationError(f"Unisender отклонил запрос ({method}, {code})")
        value = result.get("result")
        if method == "sendTestEmail" and not isinstance(value, dict) and result.get("message"):
            return {"message": str(result["message"])}
        if not isinstance(value, dict):
            raise IntegrationError(f"Unisender не вернул результат ({method})")
        return value

    def send_message(
        self,
        store: OutreachStore,
        lead_key: str,
        address: str,
        subject: str,
        body: str,
        contact_name: str = "",
    ) -> ProviderSendResult:
        destination = normalize_destination("email", address)
        if not is_single_destination("email", destination):
            raise PermissionError("Отправка заблокирована: в поле адреса больше одного получателя.")
        if not store.can_contact(lead_key, "email", destination):
            raise PermissionError("Отправка заблокирована: нет согласия или адрес подавлен.")
        gate_ready, missing = store.production_gate_ready()
        if not gate_ready:
            raise PermissionError("Производственная отправка заблокирована: " + "; ".join(missing))
        contact_fields: dict[str, object] = {
            "list_ids": self.config.unisender_list_id,
            "fields[email]": destination,
            "double_optin": 3,
            "overwrite": 0,
        }
        if contact_name.strip():
            contact_fields["fields[Name]"] = contact_name.strip()
        self._call("subscribe", contact_fields)
        message = self._call(
            "createEmailMessage",
            {
                "sender_name": self.config.sender_name,
                "sender_email": self.config.sender_email,
                "subject": subject,
                "body": plain_text_to_html(body),
                "text_body": body,
                "list_id": self.config.unisender_list_id,
                "lang": "ru",
            },
        )
        message_id = str(message.get("message_id") or "")
        if not message_id:
            raise IntegrationError("Unisender не вернул message_id")
        campaign = self._call(
            "createCampaign",
            {
                "message_id": message_id,
                "contacts": destination,
                "track_read": 0,
                "track_links": 0,
            },
        )
        campaign_id = str(campaign.get("campaign_id") or "")
        if not campaign_id:
            raise IntegrationError("Unisender не вернул campaign_id")
        return ProviderSendResult(message_id, campaign_id, "sent")

    def campaign_common_stats(self, campaign_id: str) -> dict[str, object]:
        return self._call("getCampaignCommonStats", {"campaign_id": campaign_id})

    def send_test_message(self, subject: str, body: str) -> str:
        self.validate()
        message = self._call(
            "createEmailMessage",
            {
                "sender_name": self.config.sender_name,
                "sender_email": self.config.sender_email,
                "subject": subject,
                "body": plain_text_to_html(body),
                "text_body": body,
                "list_id": self.config.unisender_list_id,
                "lang": "ru",
            },
        )
        message_id = str(message.get("message_id") or "")
        if not message_id:
            raise IntegrationError("Unisender не вернул message_id тестового письма")
        self._call(
            "sendTestEmail",
            {"id": message_id, "email": self.config.sender_email},
        )
        return message_id

    def sync_message(self, store: OutreachStore, message: dict[str, object]) -> int:
        campaign_id = str(message.get("provider_campaign_id") or "")
        if not campaign_id:
            return 0
        stats = self.campaign_common_stats(campaign_id)
        event_type = ""
        if _positive_count(stats, "spam", "complaints", "complaint"):
            event_type = "complaint"
        elif _positive_count(stats, "unsubscribed", "unsubscribe"):
            event_type = "unsubscribe"
        elif _positive_count(stats, "delivery_errors", "hard_bounces", "bounced"):
            event_type = "hard_bounce"
        elif _positive_count(stats, "delivered", "delivered_count"):
            event_type = "delivered"
        recipient = _recipient_address_for_message(store, int(message["id"]))
        if event_type:
            inserted = store.record_event(
                event_type,
                f"unisender:{campaign_id}:{event_type}",
                "email",
                address=recipient,
                lead_key=str(message["lead_key"]),
                message_id=int(message["id"]),
                payload={"campaign_id": campaign_id, "summary": _numeric_stats(stats)},
            )
            return int(inserted)
        return self._sync_delivery_report(store, message, recipient)

    def _sync_delivery_report(
        self,
        store: OutreachStore,
        message: dict[str, object],
        recipient: str,
    ) -> int:
        campaign_id = str(message["provider_campaign_id"])
        setting_key = f"unisender_delivery_task:{campaign_id}"
        task_value = store.get_setting(setting_key)
        if task_value.startswith("done:"):
            return 0
        if not task_value:
            task = self._call("async/getCampaignDeliveryStats", {"campaign_id": campaign_id})
            task_uuid = str(task.get("task_uuid") or "")
            if not task_uuid:
                raise IntegrationError("Unisender не вернул task_uuid отчёта доставки")
            store.set_setting(setting_key, task_uuid)
            return 0
        task = self._call("async/getTaskResult", {"task_uuid": task_value})
        if str(task.get("status") or "") != "completed":
            return 0
        download_url = str(task.get("file_to_download") or "")
        if not download_url.startswith("https://"):
            raise IntegrationError("Unisender не вернул безопасную ссылку на отчёт доставки")
        try:
            response = self.session.get(download_url, timeout=30)
            response.raise_for_status()
            raw = bytes(response.content)
        except requests.RequestException as error:
            raise _safe_error("Не удалось получить отчёт доставки Unisender", getattr(error, "response", None)) from error
        if raw.startswith(b"\x1f\x8b"):
            raw = gzip.decompress(raw)
        report_text = _decode_delivery_report(raw)
        report_event = _delivery_report_event(report_text)
        store.set_setting(setting_key, f"done:{task_value}")
        if not report_event:
            return 0
        return int(
            store.record_event(
                report_event,
                f"unisender:{campaign_id}:report:{report_event}",
                "email",
                address=recipient,
                lead_key=str(message["lead_key"]),
                message_id=int(message["id"]),
                payload={"campaign_id": campaign_id, "source": "delivery_report"},
            )
        )


def _positive_count(stats: dict[str, object], *keys: str) -> bool:
    for key in keys:
        raw = stats.get(key)
        if isinstance(raw, dict):
            raw = raw.get("count")
        try:
            if int(raw or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _numeric_stats(stats: dict[str, object]) -> dict[str, int]:
    safe: dict[str, int] = {}
    for key, value in stats.items():
        raw = value.get("count") if isinstance(value, dict) else value
        try:
            safe[str(key)] = int(raw)
        except (TypeError, ValueError):
            continue
    return safe


def _decode_delivery_report(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _delivery_report_event(report_text: str) -> str:
    normalized = report_text.casefold()
    if any(status in normalized for status in ("err_dest_invalid", "err_not_available")):
        return "hard_bounce"
    if any(status in normalized for status in ("err_unsubscribed", "ok_unsubscribed")):
        return "unsubscribe"
    if any(
        status in normalized
        for status in ("ok_delivered", "ok_read", "ok_link_visited", "ok_fbl", "ok_spam_folder")
    ):
        return "delivered"
    return ""


def _recipient_address_for_message(store: OutreachStore, message_id: int) -> str:
    with store._connect() as connection:
        row = connection.execute(
            """
            SELECT r.address FROM outreach_messages m
            LEFT JOIN outreach_recipients r ON r.id = m.recipient_id
            WHERE m.id = ?
            """,
            (message_id,),
        ).fetchone()
    return str(row["address"] or "") if row else ""


def _open_imap(host: str) -> imaplib.IMAP4_SSL:
    """Открывает IMAP-соединение с таймаутом.

    Без таймаута зависший или медленный сервер блокирует worker в режиме `--loop`
    навсегда: процесс не падает, поэтому в журнале не появляется ни строки, а
    синхронизация ответов и авто-пауза кампании молча перестают работать.
    """
    return imaplib.IMAP4_SSL(host, timeout=IMAP_TIMEOUT_SECONDS)


class ImapReplyClient:
    def __init__(self, config: OutreachConfig, client_factory: Any = _open_imap):
        self.config = config
        self.client_factory = client_factory

    def validate(self) -> None:
        if not self.config.imap_ready:
            raise IntegrationError("IMAP не настроен полностью")

    def sync(self, store: OutreachStore, limit: int = 100) -> int:
        self.validate()
        processed = 0
        try:
            client = self.client_factory(self.config.imap_host)
        except OSError as error:
            # Таймаут и сетевые сбои — не подкласс imaplib.IMAP4.error. Без перевода
            # в IntegrationError они пролетали бы мимо обработчиков worker и роняли
            # --loop вместо того, чтобы пережить цикл и повторить позже.
            raise IntegrationError("Не удалось подключиться к IMAP") from error
        try:
            client.login(self.config.imap_username, self.config.imap_password)
            status, _ = client.select("INBOX")
            if status != "OK":
                raise IntegrationError("Не удалось открыть INBOX")
            status, payload = client.search(None, "UNSEEN")
            if status != "OK":
                raise IntegrationError("Не удалось получить новые письма")
            ids = (payload[0] or b"").split()[:limit]
            for message_number in ids:
                status, raw_parts = client.fetch(message_number, "(RFC822)")
                if status != "OK":
                    # Сбой выборки может быть временным, поэтому письмо остаётся
                    # непрочитанным и попадёт в следующий цикл.
                    LOGGER.warning(
                        "Письмо %s не удалось получить, попробуем в следующий раз",
                        message_number.decode(errors="replace"),
                    )
                    continue
                raw = next(
                    (part[1] for part in raw_parts if isinstance(part, tuple) and len(part) > 1),
                    None,
                )
                if raw is not None and len(raw) > MAX_MESSAGE_BYTES:
                    # Размером письма распоряжается отправитель, а ящик открыт для
                    # любого. Обрезаем до лимита, но разбор продолжаем: отправитель и
                    # Message-ID лежат в заголовках, поэтому ответ лида с тяжёлой
                    # подписью или вложением всё равно остановит кампанию — теряется
                    # только хвост тела, а не факт ответа.
                    LOGGER.warning(
                        "Письмо %s больше %s байт, тело обрезано",
                        message_number.decode(errors="replace"),
                        MAX_MESSAGE_BYTES,
                    )
                    raw = raw[:MAX_MESSAGE_BYTES]
                if not raw:
                    # Пустой ответ сам не исправится: помечаем прочитанным, иначе
                    # письмо навсегда занимает место в окне UNSEEN. Номер остаётся
                    # в журнале, чтобы оператор нашёл письмо в ящике руками.
                    LOGGER.warning(
                        "Письмо %s без содержимого, разберите вручную",
                        message_number.decode(errors="replace"),
                    )
                    client.store(message_number, "+FLAGS", "\\Seen")
                    continue
                try:
                    message = email.message_from_bytes(raw)
                    event = parse_incoming_email(message)
                except Exception as error:
                    # Перехват намеренно широкий. Письмо приходит от кого угодно, а
                    # разбор идёт через email-пакет со своей иерархией исключений:
                    # LookupError от неизвестной кодировки, HeaderParseError от битого
                    # base64 в теме, CharsetError от неascii-имени кодировки — ни один
                    # из них не наследуется от общего предка с остальными. Перечислять
                    # типы значит каждый раз узнавать о новом из упавшего worker.
                    # Письмо помечается прочитанным, чтобы не занять место в окне навсегда,
                    # но его номер идёт в журнал: автоматика такой ответ не увидит,
                    # и решение по нему принимает оператор.
                    LOGGER.warning(
                        "Письмо %s не разобрано (%s), проверьте его вручную",
                        message_number.decode(errors="replace"),
                        type(error).__name__,
                    )
                    client.store(message_number, "+FLAGS", "\\Seen")
                    continue
                if event["address"] == normalize_destination("email", self.config.sender_email):
                    client.store(message_number, "+FLAGS", "\\Seen")
                    continue
                lead_key = store.find_lead_by_email(str(event["address"]))
                if not lead_key:
                    # Письмо от неизвестного адреса тоже помечается прочитанным.
                    # Иначе оно навсегда остаётся в выборке UNSEEN, а она обрезается
                    # по limit — накопившись, чужие письма вытеснят из окна реальные
                    # ответы лидов, и цепочка продолжит идти уже ответившему человеку.
                    client.store(message_number, "+FLAGS", "\\Seen")
                    continue
                inserted = store.record_event(
                    "reply",
                    str(event["provider_event_id"]),
                    "email",
                    address=str(event["address"]),
                    lead_key=lead_key,
                    payload={
                        "subject": event["subject"],
                        "body": event["body"],
                        "from_name": event["from_name"],
                    },
                    occurred_at=event["occurred_at"],
                )
                if inserted:
                    processed += 1
                client.store(message_number, "+FLAGS", "\\Seen")
        except (imaplib.IMAP4.error, OSError) as error:
            # OSError покрывает socket.timeout и обрывы соединения на login/fetch:
            # для worker это такая же временная помеха, как ошибка протокола.
            raise IntegrationError("Ошибка авторизации или синхронизации IMAP") from error
        finally:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
        return processed


def parse_incoming_email(message: Message) -> dict[str, object]:
    name, address = parseaddr(message.get("From", ""))
    subject = str(make_header(decode_header(message.get("Subject", ""))))
    message_id = message.get("Message-ID") or message.get("Message-Id")
    if not message_id:
        digest = json.dumps(
            {"from": address, "subject": subject, "date": message.get("Date", "")},
            sort_keys=True,
        )
        message_id = "generated:" + hashlib.sha256(digest.encode("utf-8")).hexdigest()
    occurred_at = datetime.now(UTC)
    if message.get("Date"):
        try:
            occurred_at = parsedate_to_datetime(message.get("Date")).astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            pass
    return {
        "address": normalize_destination("email", address),
        "from_name": str(make_header(decode_header(name))) if name else "",
        "subject": subject,
        "body": _message_text(message)[:MAX_BODY_CHARS],
        "provider_event_id": f"imap:{message_id.strip()}",
        "occurred_at": occurred_at,
    }


def _message_text(message: Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() != "text/plain" or "attachment" in str(part.get("Content-Disposition", "")):
                continue
            payload = part.get_payload(decode=True)
            if payload is not None:
                return payload[:MAX_BODY_CHARS * 4].decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )[:MAX_BODY_CHARS].strip()
        return ""
    payload = message.get_payload(decode=True)
    if payload is None:
        return str(message.get_payload() or "")[:MAX_BODY_CHARS].strip()
    # Обрезаем до декодирования: расшифровывать мегабайты, чтобы затем оставить
    # первые тысячи символов, — работа впустую за счёт отправителя.
    return payload[:MAX_BODY_CHARS * 4].decode(
        message.get_content_charset() or "utf-8", errors="replace"
    )[:MAX_BODY_CHARS].strip()


class TelegramBotClient:
    def __init__(
        self,
        config: OutreachConfig,
        session: requests.Session | Any | None = None,
        base_url: str = "https://api.telegram.org",
    ):
        self.config = config
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")

    def validate(self) -> None:
        if not self.config.telegram_ready:
            raise IntegrationError("Telegram не настроен полностью")

    def _call(self, method: str, payload: dict[str, object]) -> dict[str, object]:
        self.validate()
        try:
            response = self.session.post(
                f"{self.base_url}/bot{self.config.telegram_bot_token}/{method}",
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as error:
            if getattr(getattr(error, "response", None), "status_code", None) == 403:
                raise TelegramBlockedError("Telegram сообщил о блокировке бота пользователем") from error
            raise _safe_error("Ошибка Telegram", getattr(error, "response", None)) from error
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise IntegrationError("Telegram вернул некорректный ответ") from error
        if not isinstance(data, dict) or not data.get("ok"):
            raise IntegrationError("Telegram отклонил запрос")
        result = data.get("result")
        if method == "getUpdates":
            return {"updates": result if isinstance(result, list) else []}
        return result if isinstance(result, dict) else {}

    def get_updates(self, offset: int) -> list[dict[str, object]]:
        return list(self._call("getUpdates", {"offset": offset, "timeout": 0})["updates"])

    def send_message(self, chat_id: str, text: str, keyboard: bool = False) -> None:
        payload: dict[str, object] = {"chat_id": chat_id, "text": text}
        if keyboard:
            payload["reply_markup"] = {
                "keyboard": [[{"text": "Получить демо"}, {"text": "Задать вопрос"}], [{"text": "Не получать сообщения"}]],
                "resize_keyboard": True,
                "one_time_keyboard": False,
            }
        self._call("sendMessage", payload)

    def sync(self, store: OutreachStore) -> int:
        offset = int(store.get_setting("telegram_update_offset", "0"))
        processed = 0
        for update in self.get_updates(offset):
            update_id = int(update.get("update_id", 0))
            offset = max(offset, update_id + 1)
            member_update = update.get("my_chat_member")
            if isinstance(member_update, dict):
                chat = member_update.get("chat")
                membership = member_update.get("new_chat_member")
                if isinstance(chat, dict) and isinstance(membership, dict) and "id" in chat:
                    chat_id = str(chat["id"])
                    if str(membership.get("status") or "") in ("kicked", "left"):
                        lead_key = store.telegram_lead_for_chat(chat_id)
                        if lead_key:
                            store.upsert_permission(
                                lead_key,
                                "telegram",
                                f"tg:{chat_id}",
                                "withdrawn",
                                source="telegram_bot",
                                evidence="Бот заблокирован пользователем",
                            )
                        store.add_suppression(
                            "telegram", f"tg:{chat_id}", "blocked", "telegram_bot"
                        )
                        processed += int(
                            store.record_telegram_message(
                                update_id, chat_id, lead_key, "system", "bot_blocked"
                            )
                        )
                continue
            raw_message = update.get("message")
            if not isinstance(raw_message, dict):
                continue
            chat = raw_message.get("chat")
            if not isinstance(chat, dict) or "id" not in chat:
                continue
            chat_id = str(chat["id"])
            text = str(raw_message.get("text") or "").strip()
            lead_key = store.telegram_lead_for_chat(chat_id)
            if text.startswith("/start "):
                token = text.split(maxsplit=1)[1].strip()
                lead_key = store.consume_telegram_link(token, chat_id, self.config.link_secret)
                if lead_key:
                    if store.record_telegram_message(update_id, chat_id, lead_key, "inbound", text):
                        processed += 1
                    self.send_message(
                        chat_id,
                        "Диалог открыт. Выберите действие или напишите вопрос — ответит специалист.",
                        keyboard=True,
                    )
                    continue
            if not store.record_telegram_message(update_id, chat_id, lead_key, "inbound", text):
                continue
            processed += 1
            if text.casefold() in {"не получать сообщения", "/stop", "стоп"}:
                if lead_key:
                    store.upsert_permission(
                        lead_key,
                        "telegram",
                        f"tg:{chat_id}",
                        "withdrawn",
                        source="telegram_bot",
                        evidence="Отказ в Telegram",
                    )
                store.add_suppression("telegram", f"tg:{chat_id}", "withdrawn", "telegram_bot")
                self.send_message(chat_id, "Сообщения остановлены.")
        store.set_setting("telegram_update_offset", offset)
        return processed

    def reply(self, store: OutreachStore, chat_id: str, text: str) -> None:
        if not text.strip():
            raise ValueError("Нельзя отправить пустой ответ.")
        lead_key = store.telegram_lead_for_chat(chat_id)
        if not lead_key or not store.can_contact(lead_key, "telegram", f"tg:{chat_id}"):
            raise PermissionError("Ответ заблокирован: нет активного входящего диалога.")
        try:
            self.send_message(chat_id, text)
        except TelegramBlockedError:
            store.upsert_permission(
                lead_key,
                "telegram",
                f"tg:{chat_id}",
                "withdrawn",
                source="telegram_bot",
                evidence="Telegram отклонил отправку: бот заблокирован",
            )
            raise
        outbound_id = -int(datetime.now(UTC).timestamp() * 1_000_000)
        store.record_telegram_message(outbound_id, chat_id, lead_key, "outbound", text)


def sync_unisender_messages(store: OutreachStore, provider: UnisenderProvider, limit: int = 100) -> int:
    candidates = store.list_messages_pending_sync(limit)
    changed = 0
    for message in candidates:
        changed += provider.sync_message(store, message)
        # Отметка ставится и когда статус не изменился: иначе письмо, застрявшее
        # в 'delivered' навсегда, занимало бы место в окне каждой синхронизации.
        store.mark_message_synced(int(message["id"]))
    return changed


def configure_outreach_logging() -> None:
    logging.getLogger("urllib3").setLevel(logging.WARNING)
