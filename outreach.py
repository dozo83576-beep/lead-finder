from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lead_finder import Lead, WebsiteAudit


LOGGER = logging.getLogger("lead_finder.outreach")
PERMISSION_STATUSES = ("unknown", "consented", "inbound", "withdrawn")
ALLOWED_PERMISSION_STATUSES = ("consented", "inbound")
CAMPAIGN_STATES = ("draft", "approved", "active", "paused", "completed")
RECIPIENT_STATES = ("active", "paused", "replied", "suppressed", "completed", "failed")
MESSAGE_STATES = (
    "draft",
    "sending",
    "sent",
    "delivered",
    "replied",
    "bounced",
    "unsubscribed",
    "complained",
    "failed",
    "cancelled",
)
STOP_EVENT_TYPES = ("reply", "unsubscribe", "complaint", "hard_bounce")
SEGMENTS = ("no_site", "existing_site")
TEMPLATE_VERSION = "v1"
DEFAULT_STEP_DELAYS = (0, 3, 8, 13)
UTC = timezone.utc


@dataclass(frozen=True)
class RenderedMessage:
    step_index: int
    subject: str
    body: str


@dataclass(frozen=True)
class ProviderSendResult:
    provider_message_id: str
    provider_campaign_id: str
    status: str = "sent"


@dataclass(frozen=True)
class WorkerResult:
    sent: int
    skipped: int
    failed: int
    previews: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class OutreachConfig:
    unisender_api_key: str = ""
    unisender_list_id: str = ""
    sender_name: str = ""
    sender_email: str = ""
    reply_to: str = ""
    imap_host: str = "imap.yandex.ru"
    imap_username: str = ""
    imap_password: str = ""
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    link_secret: str = ""

    @classmethod
    def from_env(cls, environ: dict[str, str]) -> "OutreachConfig":
        return cls(
            unisender_api_key=environ.get("UNISENDER_API_KEY", "").strip(),
            unisender_list_id=environ.get("UNISENDER_LIST_ID", "").strip(),
            sender_name=environ.get("OUTREACH_SENDER_NAME", "").strip(),
            sender_email=environ.get("OUTREACH_FROM_EMAIL", "").strip(),
            reply_to=environ.get("OUTREACH_REPLY_TO", "").strip(),
            imap_host=environ.get("OUTREACH_IMAP_HOST", "imap.yandex.ru").strip(),
            imap_username=environ.get("OUTREACH_IMAP_USERNAME", "").strip(),
            imap_password=environ.get("OUTREACH_IMAP_PASSWORD", "").strip(),
            telegram_bot_token=environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_bot_username=environ.get("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@"),
            link_secret=environ.get("OUTREACH_LINK_SECRET", "").strip(),
        )

    @property
    def email_ready(self) -> bool:
        return all(
            (
                self.unisender_api_key,
                self.unisender_list_id,
                self.sender_name,
                self.sender_email,
                self.reply_to,
            )
        )

    @property
    def imap_ready(self) -> bool:
        return all((self.imap_host, self.imap_username, self.imap_password))

    @property
    def telegram_ready(self) -> bool:
        return all((self.telegram_bot_token, self.telegram_bot_username, self.link_secret))


def ensure_outreach_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS contact_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_key TEXT NOT NULL,
            channel TEXT NOT NULL,
            address TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unknown',
            source TEXT NOT NULL DEFAULT '',
            evidence TEXT NOT NULL DEFAULT '',
            obtained_at TEXT,
            revoked_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (lead_key, channel, address)
        );

        CREATE TABLE IF NOT EXISTS outreach_campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            segment TEXT NOT NULL,
            timezone TEXT NOT NULL DEFAULT 'Asia/Yekaterinburg',
            daily_limit INTEGER NOT NULL DEFAULT 5,
            state TEXT NOT NULL DEFAULT 'draft',
            template_version TEXT NOT NULL DEFAULT 'v1',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS outreach_campaign_steps (
            campaign_id INTEGER NOT NULL,
            step_index INTEGER NOT NULL,
            delay_days INTEGER NOT NULL,
            subject_template TEXT NOT NULL,
            body_template TEXT NOT NULL,
            PRIMARY KEY (campaign_id, step_index),
            FOREIGN KEY (campaign_id) REFERENCES outreach_campaigns(id)
        );

        CREATE TABLE IF NOT EXISTS outreach_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            lead_key TEXT NOT NULL,
            address TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            current_step INTEGER NOT NULL DEFAULT -1,
            next_send_at TEXT,
            stop_reason TEXT NOT NULL DEFAULT '',
            enrolled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (campaign_id, lead_key, address),
            FOREIGN KEY (campaign_id) REFERENCES outreach_campaigns(id)
        );

        CREATE TABLE IF NOT EXISTS outreach_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER,
            recipient_id INTEGER,
            lead_key TEXT NOT NULL,
            channel TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            subject TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            provider_message_id TEXT NOT NULL DEFAULT '',
            provider_campaign_id TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL UNIQUE,
            sent_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (campaign_id) REFERENCES outreach_campaigns(id),
            FOREIGN KEY (recipient_id) REFERENCES outreach_recipients(id)
        );

        CREATE TABLE IF NOT EXISTS outreach_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER,
            lead_key TEXT,
            channel TEXT NOT NULL,
            event_type TEXT NOT NULL,
            provider_event_id TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (message_id) REFERENCES outreach_messages(id)
        );

        CREATE TABLE IF NOT EXISTS outreach_suppressions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            address TEXT NOT NULL,
            reason TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (channel, address)
        );

        CREATE TABLE IF NOT EXISTS telegram_links (
            token TEXT PRIMARY KEY,
            lead_key TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            chat_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS telegram_messages (
            update_id INTEGER PRIMARY KEY,
            chat_id TEXT NOT NULL,
            lead_key TEXT,
            direction TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS outreach_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS outreach_inbox_actions (
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            lead_key TEXT,
            classification TEXT NOT NULL,
            next_step TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (source, source_id)
        );

        CREATE INDEX IF NOT EXISTS idx_outreach_due
            ON outreach_recipients(state, next_send_at);
        CREATE INDEX IF NOT EXISTS idx_outreach_message_provider
            ON outreach_messages(provider_campaign_id);
        CREATE INDEX IF NOT EXISTS idx_permission_address
            ON contact_permissions(channel, address);
        """
    )
    existing = {row[1] for row in connection.execute("PRAGMA table_info(outreach_messages)")}
    if "last_sync_at" not in existing:
        connection.execute("ALTER TABLE outreach_messages ADD COLUMN last_sync_at TEXT")


def normalize_destination(channel: str, address: str) -> str:
    value = (address or "").strip()
    if channel == "email":
        return value.lower()
    if channel == "telegram":
        if value.startswith("tg:"):
            return value
        lowered = value.lower().rstrip("/")
        for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/", "http://telegram.me/"):
            lowered = lowered.removeprefix(prefix)
        return lowered.removeprefix("@")
    return value


def sync_unknown_permissions(
    connection: sqlite3.Connection,
    lead_keys: list[str] | None = None,
) -> None:
    where = ""
    parameters: tuple[object, ...] = ()
    if lead_keys:
        placeholders = ",".join("?" for _ in lead_keys)
        where = f" WHERE lead_key IN ({placeholders})"
        parameters = tuple(lead_keys)
    rows = connection.execute(
        f"SELECT lead_key, email, social FROM leads{where}", parameters
    ).fetchall()
    permissions: list[tuple[str, str, str]] = []
    for row in rows:
        if str(row["email"] or "").strip():
            permissions.append(
                (str(row["lead_key"]), "email", normalize_destination("email", str(row["email"])))
            )
        social = str(row["social"] or "").strip()
        lowered = social.lower()
        if lowered.startswith(("@", "https://t.me/", "http://t.me/", "https://telegram.me/", "http://telegram.me/")):
            permissions.append(
                (str(row["lead_key"]), "telegram", normalize_destination("telegram", social))
            )
    connection.executemany(
        """
        INSERT OR IGNORE INTO contact_permissions (lead_key, channel, address, status)
        VALUES (?, ?, ?, 'unknown')
        """,
        permissions,
    )


def segment_for_lead(lead: Lead) -> str | None:
    audit = lead.audit
    if lead.verification_status == "confirmed_no_site":
        return "no_site"
    if audit and audit.state in ("social", "broken"):
        return "no_site"
    if audit and audit.state == "reachable" and confirmed_observation(lead):
        return "existing_site"
    return None


def _website_label(lead: Lead) -> str:
    raw = lead.website or (lead.audit.normalized_url if lead.audit else "")
    host = urlparse(raw if "://" in raw else f"https://{raw}").netloc
    return host.removeprefix("www.") or "сайт компании"


def confirmed_observation(lead: Lead) -> str:
    audit = lead.audit
    if lead.verification_status == "confirmed_no_site":
        return "В открытых источниках отсутствие отдельного сайта подтверждено вручную"
    if not audit:
        return ""
    if audit.state == "social":
        return "В открытых источниках вместо отдельного сайта указана только страница в соцсети"
    if audit.state == "broken":
        return "Указанный в открытых источниках сайт сейчас не открывается"
    if audit.state != "reachable":
        return ""
    label = _website_label(lead)
    if not audit.https:
        return f"Сайт {label} работает без защищённого HTTPS"
    if audit.mobile_viewport is False:
        return f"На сайте {label} не обнаружена базовая мобильная адаптация"
    if audit.contact_action is False:
        return f"На сайте {label} не обнаружено заметного действия для обращения клиента"
    if audit.mobile_score is not None and audit.mobile_score < 70:
        return f"Проверенная мобильная скорость сайта {label} составляет {audit.mobile_score} из 100"
    if audit.title_present is False or audit.description_present is False:
        return f"На сайте {label} не заполнены базовые SEO-метаданные"
    return ""


def render_sequence(lead: Lead, segment: str | None = None) -> list[RenderedMessage]:
    selected_segment = segment or segment_for_lead(lead)
    if selected_segment not in SEGMENTS:
        raise ValueError("Для лида нет подтверждённого основания для выбранной цепочки.")
    observation = confirmed_observation(lead)
    if not observation:
        raise ValueError("Нельзя подготовить обращение без подтверждённого наблюдения.")

    company = lead.name.strip()
    niche = lead.category.strip() or "вашей ниши"
    if selected_segment == "no_site":
        first_subject = f"сайт для {company}"
        first_body = (
            f"{company}, посмотрел открытые данные о компании. {observation}. "
            "Из-за этого потенциальному клиенту может быть сложнее быстро увидеть услуги, "
            f"примеры работ и способ связаться. Могу прислать короткий пример сайта для {niche} "
            "и перечень того, что потребуется для запуска?"
        )
        followup_focus = "пример сайта"
    else:
        first_subject = f"вопрос по сайту {company}"
        first_body = (
            f"{company}, посмотрел ваш сайт. {observation}. "
            "Это может усложнять посетителю быстрый переход от просмотра к обращению, "
            "особенно с телефона. Могу прислать короткий список точечных улучшений и пример "
            f"того, как это можно оформить для {niche}?"
        )
        followup_focus = "список улучшений"

    return [
        RenderedMessage(0, first_subject, first_body),
        RenderedMessage(
            1,
            f"коротко по {company}",
            f"{company}, возвращаюсь к предыдущему сообщению. Если задача ещё актуальна, "
            f"подготовлю {followup_focus} без презентации и долгого созвона. Ответьте, и я пришлю материал.",
        ),
        RenderedMessage(
            2,
            f"по сайту {company}",
            f"{company}, уточню в последний раз перед закрытием задачи: имеет смысл прислать "
            f"{followup_focus} сейчас или лучше вернуться к вопросу позже?",
        ),
        RenderedMessage(
            3,
            f"закрываю вопрос по {company}",
            f"{company}, закрываю вопрос, чтобы больше не отвлекать. Если тема станет актуальна, "
            "достаточно ответить на любое из моих сообщений.",
        ),
    ]


def plain_text_to_html(value: str) -> str:
    paragraphs = [f"<p>{html.escape(part.strip())}</p>" for part in value.split("\n\n") if part.strip()]
    return "".join(paragraphs)


def _is_busy_error(error: sqlite3.OperationalError) -> bool:
    """Отличает конкуренцию за запись от структурной поломки базы."""
    text = str(error).lower()
    return "locked" in text or "busy" in text


def _local_date(value: object, zone: ZoneInfo):
    """Приводит отметку времени из SQLite к локальной дате кампании.

    `sent_at` пишется как isoformat с зоной, а `created_at` заполняется значением
    CURRENT_TIMESTAMP — без зоны и через пробел, в UTC.
    """
    moment = datetime.fromisoformat(str(value).strip().replace(" ", "T"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(zone).date()


def next_business_slot(moment: datetime, timezone_name: str) -> datetime:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError("Неизвестный часовой пояс кампании.") from error
    local = moment.astimezone(zone)
    candidate = local.replace(hour=10, minute=0, second=0, microsecond=0)
    if local >= candidate.replace(hour=12):
        candidate += timedelta(days=1)
    elif local > candidate:
        candidate = local.replace(second=0, microsecond=0)
    while candidate.weekday() >= 5:
        candidate = (candidate + timedelta(days=1)).replace(hour=10, minute=0)
    return candidate.astimezone(UTC)


def is_send_window(moment: datetime, timezone_name: str) -> bool:
    try:
        local = moment.astimezone(ZoneInfo(timezone_name))
    except ZoneInfoNotFoundError as error:
        raise ValueError("Неизвестный часовой пояс кампании.") from error
    return local.weekday() < 5 and 10 <= local.hour < 12


class OutreachStore:
    def __init__(self, path: str = "leads.db"):
        self.path = path
        with self._connect() as connection:
            ensure_outreach_schema(connection)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def get_lead(self, lead_key: str) -> Lead | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM leads WHERE lead_key = ?", (lead_key,)).fetchone()
        if not row:
            return None
        audit_data = json.loads(row["audit_json"]) if row["audit_json"] else None
        return Lead(
            lead_key=row["lead_key"],
            name=row["name"],
            category=row["category"],
            city=row["city"],
            address=row["address"],
            phone=row["phone"],
            email=row["email"],
            social=row["social"],
            website=row["website"],
            source=row["source"],
            source_url=row["source_url"],
            latitude=row["latitude"],
            longitude=row["longitude"],
            website_source=row["website_source"],
            verification_status=row["verification_status"],
            verification_evidence=json.loads(row["verification_evidence_json"]),
            need_score=row["need_score"],
            contact_score=row["contact_score"],
            branch_count=row["branch_count"],
            score=row["score"],
            reasons=json.loads(row["reasons_json"]),
            status=row["status"],
            note=row["note"],
            audit=WebsiteAudit(**audit_data) if audit_data else None,
        )

    def upsert_permission(
        self,
        lead_key: str,
        channel: str,
        address: str,
        status: str,
        source: str = "",
        evidence: str = "",
        obtained_at: str | None = None,
    ) -> None:
        if status not in PERMISSION_STATUSES:
            raise ValueError("Неизвестный статус согласия.")
        destination = normalize_destination(channel, address)
        if not destination:
            raise ValueError("Не указан адрес контакта.")
        if status == "consented" and not (source.strip() and evidence.strip() and obtained_at):
            raise ValueError("Для согласия нужны источник, доказательство и дата получения.")
        if status == "inbound" and not obtained_at:
            raise ValueError("Для входящего обращения нужна дата получения.")
        revoked_at = datetime.now(UTC).isoformat() if status == "withdrawn" else None
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO contact_permissions (
                    lead_key, channel, address, status, source, evidence, obtained_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lead_key, channel, address) DO UPDATE SET
                    status = excluded.status,
                    source = excluded.source,
                    evidence = excluded.evidence,
                    obtained_at = excluded.obtained_at,
                    revoked_at = excluded.revoked_at,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    lead_key,
                    channel,
                    destination,
                    status,
                    source.strip(),
                    evidence.strip(),
                    obtained_at,
                    revoked_at,
                ),
            )
        if status == "withdrawn":
            self.add_suppression(channel, destination, "withdrawn", "permission")
            self.stop_by_destination(channel, destination, "withdrawn")

    def get_permission(self, lead_key: str, channel: str, address: str) -> dict[str, object]:
        destination = normalize_destination(channel, address)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM contact_permissions
                WHERE lead_key = ? AND channel = ? AND address = ?
                """,
                (lead_key, channel, destination),
            ).fetchone()
        if row:
            return dict(row)
        return {
            "lead_key": lead_key,
            "channel": channel,
            "address": destination,
            "status": "unknown",
            "source": "",
            "evidence": "",
            "obtained_at": None,
            "revoked_at": None,
        }

    def list_permissions(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*, l.name AS lead_name
                FROM contact_permissions p
                LEFT JOIN leads l ON l.lead_key = p.lead_key
                ORDER BY p.updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def can_contact(self, lead_key: str, channel: str, address: str) -> bool:
        permission = self.get_permission(lead_key, channel, address)
        return (
            permission["status"] in ALLOWED_PERMISSION_STATUSES
            and not self.is_suppressed(channel, address)
        )

    def add_suppression(self, channel: str, address: str, reason: str, source: str = "") -> None:
        destination = normalize_destination(channel, address)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO outreach_suppressions (channel, address, reason, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel, address) DO UPDATE SET
                    reason = excluded.reason, source = excluded.source
                """,
                (channel, destination, reason, source),
            )

    def is_suppressed(self, channel: str, address: str) -> bool:
        destination = normalize_destination(channel, address)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM outreach_suppressions WHERE channel = ? AND address = ?",
                (channel, destination),
            ).fetchone()
        return bool(row)

    def list_suppressions(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM outreach_suppressions ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_draft(self, lead: Lead) -> int:
        sequence = render_sequence(lead)
        first = sequence[0]
        key = f"draft:{lead.lead_key}:{segment_for_lead(lead)}:{TEMPLATE_VERSION}:0"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO outreach_messages (
                    lead_key, channel, step_index, subject, body, status, idempotency_key
                ) VALUES (?, 'email', 0, ?, ?, 'draft', ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    subject = excluded.subject, body = excluded.body, updated_at = CURRENT_TIMESTAMP
                """,
                (lead.lead_key, first.subject, first.body, key),
            )
            row = connection.execute(
                "SELECT id FROM outreach_messages WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return int(row["id"])

    def list_drafts(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*, l.name AS lead_name
                FROM outreach_messages m
                LEFT JOIN leads l ON l.lead_key = m.lead_key
                WHERE m.status = 'draft' AND m.campaign_id IS NULL
                ORDER BY m.updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_campaign(
        self,
        name: str,
        segment: str,
        timezone_name: str = "Asia/Yekaterinburg",
        daily_limit: int = 5,
    ) -> int:
        if not name.strip():
            raise ValueError("Укажите название кампании.")
        if segment not in SEGMENTS:
            raise ValueError("Неизвестный сегмент кампании.")
        if not 1 <= daily_limit <= 5:
            raise ValueError("Стартовый дневной лимит должен быть от 1 до 5.")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Неизвестный часовой пояс кампании.") from error
        templates = _template_placeholders(segment)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO outreach_campaigns (name, segment, timezone, daily_limit)
                VALUES (?, ?, ?, ?)
                """,
                (name.strip(), segment, timezone_name, daily_limit),
            )
            campaign_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO outreach_campaign_steps (
                    campaign_id, step_index, delay_days, subject_template, body_template
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (campaign_id, message.step_index, DEFAULT_STEP_DELAYS[message.step_index], message.subject, message.body)
                    for message in templates
                ],
            )
        return campaign_id

    def list_campaigns(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.*,
                       COUNT(r.id) AS recipients,
                       SUM(CASE WHEN r.state = 'active' THEN 1 ELSE 0 END) AS active_recipients
                FROM outreach_campaigns c
                LEFT JOIN outreach_recipients r ON r.campaign_id = c.id
                GROUP BY c.id
                ORDER BY c.id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_campaign(self, campaign_id: int) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM outreach_campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
        return dict(row) if row else None

    def set_campaign_state(self, campaign_id: int, state: str) -> None:
        if state not in CAMPAIGN_STATES:
            raise ValueError("Неизвестное состояние кампании.")
        current = self.get_campaign(campaign_id)
        if not current:
            raise ValueError("Кампания не найдена.")
        allowed = {
            "draft": {"approved"},
            "approved": {"active", "paused"},
            "active": {"paused", "completed"},
            "paused": {"active", "completed"},
            "completed": set(),
        }
        if state != current["state"] and state not in allowed[str(current["state"])]:
            raise ValueError("Недопустимый переход состояния кампании.")
        with self._connect() as connection:
            connection.execute(
                "UPDATE outreach_campaigns SET state = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (state, campaign_id),
            )

    def increase_daily_limit(
        self,
        campaign_id: int,
        new_limit: int,
        now: datetime | None = None,
    ) -> None:
        campaign = self.get_campaign(campaign_id)
        if not campaign:
            raise ValueError("Кампания не найдена.")
        current_limit = int(campaign["daily_limit"])
        max_limit = int(current_limit * 1.25)
        if max_limit == current_limit:
            # При лимите 1-3 четверть не набирает целого адресата. Рост здесь
            # невозможен не из-за ошибки оператора, и сообщение должно это объяснять,
            # а не выглядеть как отказ без причины.
            raise ValueError(
                f"При лимите {current_limit} рост на 25% не даёт целого адресата. "
                "Создайте кампанию с бо́льшим стартовым лимитом."
            )
        if new_limit <= current_limit or new_limit > max_limit:
            raise ValueError(f"Лимит можно увеличить максимум до {max_limit} адресатов в день.")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        changed_value = self.get_setting(
            f"campaign_limit_changed_at:{campaign_id}", str(campaign["created_at"])
        )
        changed_at = datetime.fromisoformat(changed_value)
        if changed_at.tzinfo is None:
            changed_at = changed_at.replace(tzinfo=UTC)
        if current - changed_at.astimezone(UTC) < timedelta(days=7):
            raise ValueError("Лимит можно повышать не чаще одного раза в семь дней.")
        since = (current - timedelta(days=7)).isoformat()
        with self._connect() as connection:
            risk_events = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM outreach_events e
                    JOIN outreach_messages m ON m.id = e.message_id
                    WHERE m.campaign_id = ? AND e.event_type IN ('complaint', 'hard_bounce')
                      AND datetime(e.occurred_at) >= datetime(?)
                    """,
                    (campaign_id, since),
                ).fetchone()[0]
            )
            failures = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM outreach_messages
                    WHERE campaign_id = ? AND status = 'failed'
                      AND datetime(updated_at) >= datetime(?)
                    """,
                    (campaign_id, since),
                ).fetchone()[0]
            )
            if risk_events or failures:
                raise ValueError("Повышение лимита заблокировано из-за ошибок или жалоб за семь дней.")
            connection.execute(
                "UPDATE outreach_campaigns SET daily_limit = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_limit, campaign_id),
            )
        self.set_setting(f"campaign_limit_changed_at:{campaign_id}", current.isoformat())

    def enroll_recipient(self, campaign_id: int, lead: Lead, now: datetime | None = None) -> int:
        campaign = self.get_campaign(campaign_id)
        if not campaign:
            raise ValueError("Кампания не найдена.")
        if segment_for_lead(lead) != campaign["segment"]:
            raise ValueError("Лид не соответствует подтверждённому сегменту кампании.")
        if not lead.email:
            raise ValueError("У лида нет email.")
        if not self.can_contact(lead.lead_key, "email", lead.email):
            raise PermissionError("Отправка заблокирована: нет согласия или адрес подавлен.")
        send_at = next_business_slot(now or datetime.now(UTC), str(campaign["timezone"]))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO outreach_recipients (
                    campaign_id, lead_key, address, state, next_send_at
                ) VALUES (?, ?, ?, 'active', ?)
                ON CONFLICT(campaign_id, lead_key, address) DO NOTHING
                """,
                (campaign_id, lead.lead_key, normalize_destination("email", lead.email), send_at.isoformat()),
            )
            row = connection.execute(
                """
                SELECT id FROM outreach_recipients
                WHERE campaign_id = ? AND lead_key = ? AND address = ?
                """,
                (campaign_id, lead.lead_key, normalize_destination("email", lead.email)),
            ).fetchone()
        return int(row["id"])

    def due_recipients(self, now: datetime, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT r.*, c.segment, c.timezone, c.daily_limit, c.state AS campaign_state
                FROM outreach_recipients r
                JOIN outreach_campaigns c ON c.id = r.campaign_id
                WHERE r.state = 'active' AND c.state = 'active'
                  AND r.next_send_at IS NOT NULL AND r.next_send_at <= ?
                ORDER BY r.next_send_at, r.id
                LIMIT ?
                """,
                (now.astimezone(UTC).isoformat(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def reschedule_recipient(self, recipient_id: int, moment: datetime, timezone_name: str) -> None:
        next_at = next_business_slot(moment, timezone_name)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE outreach_recipients
                SET next_send_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND state = 'active'
                """,
                (next_at.isoformat(), recipient_id),
            )

    def sent_today(self, campaign_id: int, now: datetime, timezone_name: str) -> int:
        zone = ZoneInfo(timezone_name)
        local_day = now.astimezone(zone).date()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sent_at FROM outreach_messages
                WHERE campaign_id = ? AND status IN ('sent', 'delivered', 'replied')
                  AND sent_at IS NOT NULL
                """,
                (campaign_id,),
            ).fetchall()
        return sum(
            datetime.fromisoformat(row["sent_at"]).astimezone(zone).date() == local_day for row in rows
        )

    def claim_message_within_limit(
        self,
        recipient: dict[str, object],
        rendered: RenderedMessage,
        now: datetime,
        daily_limit: int,
    ) -> dict[str, object] | None:
        """Резервирует шаг цепочки, проверяя дневной лимит в той же транзакции.

        Отдельная проверка через `sent_today()` перед `claim_message()` не защищает от
        параллельного запуска: worker в режиме `--loop` и отправка из интерфейса — штатный
        сценарий, и оба процесса успевали пройти проверку до того, как другой фиксировал
        свою отправку. `BEGIN IMMEDIATE` берёт write-lock до подсчёта, поэтому второй
        процесс ждёт и видит уже занятые слоты. Учитываются и письма в статусе `sending`:
        они заняли слот, но ещё не получили `sent_at`.
        """
        campaign_id = int(recipient["campaign_id"])
        zone = ZoneInfo(str(recipient["timezone"]))
        local_day = now.astimezone(zone).date()
        key = f"campaign:{campaign_id}:recipient:{recipient['id']}:step:{rendered.step_index}"
        try:
            return self._claim_within_limit(recipient, rendered, now, daily_limit, campaign_id, zone, local_day, key)
        except sqlite3.OperationalError as error:
            if not _is_busy_error(error):
                # Нет таблицы, нет колонки, ошибка диска — это не конкуренция за запись.
                # Проглотить такое означало бы бесконечный холостой цикл без диагностики.
                raise
            # Соседний процесс держит запись дольше busy_timeout. Слот не получен —
            # адресат ждёт следующего цикла. Ронять фоновый worker из-за этого нельзя.
            LOGGER.warning("Слот не получен, база занята соседним процессом: %s", error)
            return None

    def _claim_within_limit(
        self,
        recipient: dict[str, object],
        rendered: RenderedMessage,
        now: datetime,
        daily_limit: int,
        campaign_id: int,
        zone: ZoneInfo,
        local_day: object,
        key: str,
    ) -> dict[str, object] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT COALESCE(sent_at, created_at) AS moment FROM outreach_messages
                WHERE campaign_id = ?
                  AND status IN ('sending', 'sent', 'delivered', 'replied')
                """,
                (campaign_id,),
            ).fetchall()
            used = sum(_local_date(row["moment"], zone) == local_day for row in rows)
            if used >= daily_limit:
                return None
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO outreach_messages (
                    campaign_id, recipient_id, lead_key, channel, step_index,
                    subject, body, status, idempotency_key, created_at
                ) VALUES (?, ?, ?, 'email', ?, ?, ?, 'sending', ?, ?)
                """,
                (
                    campaign_id,
                    recipient["id"],
                    recipient["lead_key"],
                    rendered.step_index,
                    rendered.subject,
                    rendered.body,
                    key,
                    now.isoformat(),
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM outreach_messages WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row)

    def claim_message(self, recipient: dict[str, object], rendered: RenderedMessage) -> dict[str, object] | None:
        key = f"campaign:{recipient['campaign_id']}:recipient:{recipient['id']}:step:{rendered.step_index}"
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO outreach_messages (
                    campaign_id, recipient_id, lead_key, channel, step_index,
                    subject, body, status, idempotency_key
                ) VALUES (?, ?, ?, 'email', ?, ?, ?, 'sending', ?)
                """,
                (
                    recipient["campaign_id"],
                    recipient["id"],
                    recipient["lead_key"],
                    rendered.step_index,
                    rendered.subject,
                    rendered.body,
                    key,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM outreach_messages WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row)

    def mark_message_sent(
        self,
        message_id: int,
        result: ProviderSendResult,
        sent_at: datetime,
    ) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM outreach_messages WHERE id = ?", (message_id,)
            ).fetchone()
            if not row:
                raise ValueError("Сообщение не найдено.")
            connection.execute(
                """
                UPDATE outreach_messages
                SET status = ?, provider_message_id = ?, provider_campaign_id = ?,
                    sent_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    result.status,
                    result.provider_message_id,
                    result.provider_campaign_id,
                    sent_at.astimezone(UTC).isoformat(),
                    message_id,
                ),
            )
            campaign = connection.execute(
                "SELECT * FROM outreach_campaigns WHERE id = ?", (row["campaign_id"],)
            ).fetchone()
            next_index = int(row["step_index"]) + 1
            if next_index >= len(DEFAULT_STEP_DELAYS):
                connection.execute(
                    """
                    UPDATE outreach_recipients
                    SET current_step = ?, state = 'completed', next_send_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (row["step_index"], row["recipient_id"]),
                )
            else:
                delta_days = DEFAULT_STEP_DELAYS[next_index] - DEFAULT_STEP_DELAYS[int(row["step_index"])]
                next_at = next_business_slot(sent_at + timedelta(days=delta_days), campaign["timezone"])
                connection.execute(
                    """
                    UPDATE outreach_recipients
                    SET current_step = ?, next_send_at = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (row["step_index"], next_at.isoformat(), row["recipient_id"]),
                )

    def mark_message_failed(self, message_id: int, reason: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT recipient_id FROM outreach_messages WHERE id = ?", (message_id,)
            ).fetchone()
            connection.execute(
                "UPDATE outreach_messages SET status = 'failed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (message_id,),
            )
            if row and row["recipient_id"]:
                connection.execute(
                    """
                    UPDATE outreach_recipients
                    SET state = 'failed', stop_reason = ?, next_send_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (reason[:500], row["recipient_id"]),
                )

    def record_event(
        self,
        event_type: str,
        provider_event_id: str,
        channel: str,
        address: str = "",
        lead_key: str | None = None,
        message_id: int | None = None,
        payload: dict[str, object] | None = None,
        occurred_at: datetime | None = None,
    ) -> bool:
        event_at = (occurred_at or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO outreach_events (
                    message_id, lead_key, channel, event_type, provider_event_id,
                    payload_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    lead_key,
                    channel,
                    event_type,
                    provider_event_id,
                    json.dumps(payload or {}, ensure_ascii=False),
                    event_at,
                ),
            )
        if cursor.rowcount == 0:
            return False
        message_status = {
            "delivered": "delivered",
            "reply": "replied",
            "unsubscribe": "unsubscribed",
            "complaint": "complained",
            "hard_bounce": "bounced",
        }.get(event_type)
        if message_id and message_status:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE outreach_messages
                    SET status = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status NOT IN ('failed', 'cancelled')
                    """,
                    (message_status, message_id),
                )
        if event_type in STOP_EVENT_TYPES and address:
            reason = {
                "reply": "replied",
                "unsubscribe": "unsubscribed",
                "complaint": "complained",
                "hard_bounce": "bounced",
            }[event_type]
            if event_type != "reply":
                self.add_suppression(channel, address, reason, "event")
            self.stop_by_destination(channel, address, reason)
        if message_id and event_type in ("complaint", "hard_bounce"):
            self._pause_campaign_for_risk_event(message_id, event_type)
        return True

    def _pause_campaign_for_risk_event(self, message_id: int, event_type: str) -> None:
        with self._connect() as connection:
            message = connection.execute(
                "SELECT campaign_id FROM outreach_messages WHERE id = ?", (message_id,)
            ).fetchone()
            if not message or not message["campaign_id"]:
                return
            campaign_id = int(message["campaign_id"])
            recipients = int(
                connection.execute(
                    "SELECT COUNT(*) FROM outreach_recipients WHERE campaign_id = ?",
                    (campaign_id,),
                ).fetchone()[0]
            )
            risk_events = int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT e.provider_event_id)
                    FROM outreach_events e
                    JOIN outreach_messages m ON m.id = e.message_id
                    WHERE m.campaign_id = ? AND e.event_type = ?
                    """,
                    (campaign_id, event_type),
                ).fetchone()[0]
            )
            should_pause = event_type == "complaint" or (
                event_type == "hard_bounce" and recipients <= 20 and risk_events >= 2
            )
            if should_pause:
                connection.execute(
                    """
                    UPDATE outreach_campaigns
                    SET state = 'paused', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND state = 'active'
                    """,
                    (campaign_id,),
                )

    def stop_by_destination(self, channel: str, address: str, reason: str) -> None:
        destination = normalize_destination(channel, address)
        recipient_state = "replied" if reason == "replied" else "suppressed"
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE outreach_recipients
                SET state = ?, stop_reason = ?, next_send_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE address = ? AND state = 'active'
                """,
                (recipient_state, reason, destination),
            )
            status = {
                "replied": "replied",
                "unsubscribed": "unsubscribed",
                "complained": "complained",
                "bounced": "bounced",
                "withdrawn": "cancelled",
            }.get(reason, "cancelled")
            connection.execute(
                """
                UPDATE outreach_messages
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE recipient_id IN (
                    SELECT id FROM outreach_recipients WHERE address = ?
                ) AND status IN ('draft', 'sending', 'sent', 'delivered')
                """,
                (status, destination),
            )

    def list_messages(self, limit: int = 200) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*, l.name AS lead_name
                FROM outreach_messages m
                LEFT JOIN leads l ON l.lead_key = m.lead_key
                ORDER BY m.id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_messages_pending_sync(self, limit: int = 100) -> list[dict[str, object]]:
        """Отдаёт письма, по которым ещё ждём delivery-report провайдера.

        Фильтр по статусу применяется в SQL, а не после выборки: иначе при росте базы
        старые `sent` письма выпадали бы из окна синхронизации и авто-пауза кампании
        по complaint и hard bounce для них переставала бы срабатывать.

        Окно вращается по давности последней проверки, а не по id. Статусы `sent` и
        `delivered` терминальными не являются, но письмо может остаться в них навсегда —
        при сортировке по id такие письма заняли бы всё окно и голодать начали бы уже
        новые. Ни разу не проверенные (`last_sync_at IS NULL`) идут первыми.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*, l.name AS lead_name
                FROM outreach_messages m
                LEFT JOIN leads l ON l.lead_key = m.lead_key
                WHERE m.channel = 'email'
                  AND m.provider_campaign_id <> ''
                  AND m.status IN ('sent', 'delivered')
                ORDER BY COALESCE(m.last_sync_at, '') ASC, m.id ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_message_synced(self, message_id: int, moment: datetime | None = None) -> None:
        """Отмечает, что письмо только что сверялось с провайдером."""
        stamp = (moment or datetime.now(UTC)).isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE outreach_messages SET last_sync_at = ? WHERE id = ?",
                (stamp, message_id),
            )

    def list_events(self, limit: int = 200) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM outreach_events ORDER BY occurred_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def set_inbox_action(
        self,
        source: str,
        source_id: str | int,
        lead_key: str | None,
        classification: str,
        next_step: str = "",
    ) -> None:
        if classification not in ("positive", "question", "neutral", "negative"):
            raise ValueError("Неизвестная классификация ответа.")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO outreach_inbox_actions (
                    source, source_id, lead_key, classification, next_step
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source, source_id) DO UPDATE SET
                    lead_key = excluded.lead_key,
                    classification = excluded.classification,
                    next_step = excluded.next_step,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (source, str(source_id), lead_key, classification, next_step.strip()),
            )

    def list_inbox_actions(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM outreach_inbox_actions ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delivery_metrics(self) -> dict[str, int]:
        with self._connect() as connection:
            message_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM outreach_messages GROUP BY status"
            ).fetchall()
            event_rows = connection.execute(
                "SELECT event_type, COUNT(*) AS count FROM outreach_events GROUP BY event_type"
            ).fetchall()
        metrics = {row["status"]: int(row["count"]) for row in message_rows}
        metrics.update({row["event_type"]: int(row["count"]) for row in event_rows})
        return metrics

    def set_setting(self, key: str, value: str | bool | int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO outreach_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (key, str(int(value)) if isinstance(value, bool) else str(value)),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM outreach_settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def production_gate_ready(self) -> tuple[bool, list[str]]:
        checks = {
            "dns_verified": "SPF/DKIM/DMARC не подтверждены",
            "unsubscribe_verified": "отписка не проверена",
            "seed_delivery_verified": "контрольная доставка не проверена",
            "production_enabled": "производственная отправка не разрешена",
        }
        missing = [message for key, message in checks.items() if self.get_setting(key, "0") != "1"]
        return not missing, missing

    def create_telegram_link(
        self,
        lead_key: str,
        bot_username: str,
        secret: str,
        expires_days: int = 14,
    ) -> str:
        if not (bot_username.strip() and secret):
            raise ValueError("Telegram bot username и секрет ссылки не настроены.")
        nonce = secrets.token_urlsafe(12)
        signature = hmac.new(secret.encode("utf-8"), nonce.encode("ascii"), hashlib.sha256).hexdigest()[:16]
        token = f"{nonce}.{signature}"
        expires_at = datetime.now(UTC) + timedelta(days=expires_days)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO telegram_links (token, lead_key, expires_at) VALUES (?, ?, ?)",
                (token, lead_key, expires_at.isoformat()),
            )
        return f"https://t.me/{bot_username.lstrip('@')}?start={token}"

    def consume_telegram_link(self, token: str, chat_id: str, secret: str) -> str | None:
        try:
            nonce, supplied = token.rsplit(".", 1)
        except ValueError:
            return None
        expected = hmac.new(secret.encode("utf-8"), nonce.encode("ascii"), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(expected, supplied):
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM telegram_links WHERE token = ?", (token,)
            ).fetchone()
            if not row or row["used_at"] or datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
                return None
            connection.execute(
                "UPDATE telegram_links SET used_at = ?, chat_id = ? WHERE token = ?",
                (datetime.now(UTC).isoformat(), str(chat_id), token),
            )
        self.upsert_permission(
            str(row["lead_key"]),
            "telegram",
            f"tg:{chat_id}",
            "inbound",
            source="telegram_bot",
            evidence="Пользователь самостоятельно запустил персональную ссылку Telegram",
            obtained_at=datetime.now(UTC).isoformat(),
        )
        return str(row["lead_key"])

    def telegram_lead_for_chat(self, chat_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT lead_key FROM contact_permissions
                WHERE channel = 'telegram' AND address = ? AND status = 'inbound'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (f"tg:{chat_id}",),
            ).fetchone()
        return str(row["lead_key"]) if row else None

    def record_telegram_message(
        self,
        update_id: int,
        chat_id: str,
        lead_key: str | None,
        direction: str,
        text: str,
        created_at: datetime | None = None,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO telegram_messages (
                    update_id, chat_id, lead_key, direction, text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    update_id,
                    str(chat_id),
                    lead_key,
                    direction,
                    text,
                    (created_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
                ),
            )
        return cursor.rowcount > 0

    def list_telegram_messages(self, limit: int = 200) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT t.*, l.name AS lead_name
                FROM telegram_messages t
                LEFT JOIN leads l ON l.lead_key = t.lead_key
                ORDER BY t.created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_lead_by_email(self, address: str) -> str | None:
        destination = normalize_destination("email", address)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT lead_key FROM leads WHERE lower(trim(email)) = ? LIMIT 1", (destination,)
            ).fetchone()
        return str(row["lead_key"]) if row else None


def _template_placeholders(segment: str) -> list[RenderedMessage]:
    placeholder_audit = (
        WebsiteAudit(state="missing")
        if segment == "no_site"
        else WebsiteAudit(
            state="reachable",
            normalized_url="https://example.ru",
            https=True,
            mobile_viewport=False,
        )
    )
    placeholder = Lead(
        name="{{company}}",
        lead_key="template",
        category="{{niche}}",
        website="https://example.ru" if segment == "existing_site" else "",
        verification_status="confirmed_no_site" if segment == "no_site" else "site_found",
        audit=placeholder_audit,
    )
    return render_sequence(placeholder, segment)
