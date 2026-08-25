from __future__ import annotations

import csv
import hashlib
import io
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from lead_finder import sanitize_export_cell


UTC = timezone.utc
SOURCE_KINDS = ("search", "community", "partner", "inbound", "manual")
COMMUNITY_PLATFORMS = ("telegram", "tenchat", "vk", "other")
SOURCE_STATES = ("watching", "engaging", "active", "paused")
PROFILE_STATES = ("draft", "approved")
NETWORKING_ACTIONS = (
    "observe",
    "public_comment",
    "inbound_reply",
    "partner_conversation",
    "introduction",
)
PARTNER_STATES = ("prospect", "active", "paused", "ended")
DEAL_STAGES = ("new", "qualified", "discovery", "proposal", "negotiation", "won", "lost")
OPEN_DEAL_STAGES = DEAL_STAGES[:-2]
PAYMENT_STATES = ("planned", "paid", "cancelled")
PAYOUT_STATES = ("accrued", "due", "paid", "cancelled")
TASK_STATES = ("open", "done", "cancelled")
NOTE_TYPES = ("qualification", "objection", "verified_observation")

DEAL_TRANSITIONS = {
    "new": {"qualified", "lost"},
    "qualified": {"discovery", "lost"},
    "discovery": {"proposal", "lost"},
    "proposal": {"negotiation", "lost"},
    "negotiation": {"won", "lost"},
    "won": set(),
    "lost": set(),
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _required(value: str, label: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"Не заполнено поле «{label}».")
    return cleaned


def _choice(value: str, allowed: tuple[str, ...], label: str) -> str:
    if value not in allowed:
        raise ValueError(f"Недопустимое значение поля «{label}».")
    return value


def _non_negative_int(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Поле «{label}» должно быть целым неотрицательным числом.")
    return value


def _positive_int(value: int, label: str) -> int:
    result = _non_negative_int(value, label)
    if result == 0:
        raise ValueError(f"Поле «{label}» должно быть больше нуля.")
    return result


def _as_utc(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def ensure_crm_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS acquisition_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK(kind IN ('search','community','partner','inbound','manual')),
            platform TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '',
            niche TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'watching'
                CHECK(state IN ('watching','engaging','active','paused')),
            activity_score INTEGER NOT NULL DEFAULT 0 CHECK(activity_score BETWEEN 0 AND 5),
            audience_fit_score INTEGER NOT NULL DEFAULT 0 CHECK(audience_fit_score BETWEEN 0 AND 5),
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS positioning_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            audience TEXT NOT NULL,
            value_proposition TEXT NOT NULL,
            evidence TEXT NOT NULL DEFAULT '',
            cta TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS partners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            company TEXT NOT NULL DEFAULT '',
            specialty TEXT NOT NULL DEFAULT '',
            niches TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            telegram TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'prospect'
                CHECK(state IN ('prospect','active','paused','ended')),
            relationship_evidence TEXT NOT NULL,
            default_commission_bps INTEGER NOT NULL DEFAULT 1000
                CHECK(default_commission_bps BETWEEN 0 AND 10000),
            payout_delay_days INTEGER NOT NULL DEFAULT 3 CHECK(payout_delay_days >= 0),
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS lead_crm_profiles (
            lead_key TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL CHECK(source_kind IN ('search','community','partner','inbound','manual')),
            acquisition_source_id INTEGER,
            partner_id INTEGER,
            referral_id INTEGER,
            next_step TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(lead_key) REFERENCES leads(lead_key),
            FOREIGN KEY(acquisition_source_id) REFERENCES acquisition_sources(id),
            FOREIGN KEY(partner_id) REFERENCES partners(id)
        );

        CREATE TABLE IF NOT EXISTS networking_activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            acquisition_source_id INTEGER,
            lead_key TEXT,
            partner_id INTEGER,
            action_type TEXT NOT NULL CHECK(action_type IN (
                'observe','public_comment','inbound_reply','partner_conversation','introduction'
            )),
            reference_url TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL,
            outcome TEXT NOT NULL DEFAULT '',
            occurred_at TEXT NOT NULL,
            next_task TEXT NOT NULL DEFAULT '',
            next_task_due_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(acquisition_source_id) REFERENCES acquisition_sources(id),
            FOREIGN KEY(lead_key) REFERENCES leads(lead_key),
            FOREIGN KEY(partner_id) REFERENCES partners(id)
        );

        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL UNIQUE,
            partner_id INTEGER NOT NULL,
            lead_key TEXT NOT NULL,
            acquisition_source_id INTEGER,
            introduced_at TEXT NOT NULL,
            channel TEXT NOT NULL,
            evidence TEXT NOT NULL,
            commission_bps INTEGER NOT NULL CHECK(commission_bps BETWEEN 0 AND 10000),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(partner_id) REFERENCES partners(id),
            FOREIGN KEY(lead_key) REFERENCES leads(lead_key),
            FOREIGN KEY(acquisition_source_id) REFERENCES acquisition_sources(id)
        );

        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_key TEXT NOT NULL,
            title TEXT NOT NULL,
            title_key TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'new'
                CHECK(stage IN ('new','qualified','discovery','proposal','negotiation','won','lost')),
            value_kopecks INTEGER CHECK(value_kopecks IS NULL OR value_kopecks >= 0),
            lost_reason TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL CHECK(source_kind IN ('search','community','partner','inbound','manual')),
            referral_id INTEGER,
            idempotency_key TEXT UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT,
            FOREIGN KEY(lead_key) REFERENCES leads(lead_key),
            FOREIGN KEY(referral_id) REFERENCES referrals(id)
        );

        CREATE TABLE IF NOT EXISTS deal_stage_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_id INTEGER NOT NULL,
            from_stage TEXT,
            to_stage TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            changed_at TEXT NOT NULL,
            FOREIGN KEY(deal_id) REFERENCES deals(id)
        );

        CREATE TABLE IF NOT EXISTS deal_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_id INTEGER NOT NULL,
            task_type TEXT NOT NULL DEFAULT 'other',
            description TEXT NOT NULL,
            due_at TEXT,
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','done','cancelled')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            FOREIGN KEY(deal_id) REFERENCES deals(id)
        );

        CREATE TABLE IF NOT EXISTS deal_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_id INTEGER NOT NULL,
            note_type TEXT NOT NULL CHECK(note_type IN ('qualification','objection','verified_observation')),
            summary TEXT NOT NULL,
            evidence TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(deal_id) REFERENCES deals(id)
        );

        CREATE TABLE IF NOT EXISTS deal_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_id INTEGER NOT NULL,
            document_type TEXT NOT NULL,
            number TEXT NOT NULL DEFAULT '',
            document_date TEXT,
            url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(deal_id) REFERENCES deals(id)
        );

        CREATE TABLE IF NOT EXISTS deal_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_id INTEGER NOT NULL,
            amount_kopecks INTEGER NOT NULL CHECK(amount_kopecks > 0),
            due_at TEXT,
            paid_at TEXT,
            status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','paid','cancelled')),
            external_ref TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(deal_id) REFERENCES deals(id)
        );

        CREATE TABLE IF NOT EXISTS partner_payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referral_id INTEGER NOT NULL UNIQUE,
            partner_id INTEGER NOT NULL,
            deal_id INTEGER NOT NULL,
            commission_bps INTEGER NOT NULL CHECK(commission_bps BETWEEN 0 AND 10000),
            basis_kopecks INTEGER NOT NULL CHECK(basis_kopecks >= 0),
            amount_kopecks INTEGER NOT NULL CHECK(amount_kopecks >= 0),
            due_at TEXT,
            status TEXT NOT NULL DEFAULT 'accrued'
                CHECK(status IN ('accrued','due','paid','cancelled')),
            paid_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(referral_id) REFERENCES referrals(id),
            FOREIGN KEY(partner_id) REFERENCES partners(id),
            FOREIGN KEY(deal_id) REFERENCES deals(id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_deal_per_subject
            ON deals(lead_key, title_key) WHERE stage NOT IN ('won','lost');
        CREATE INDEX IF NOT EXISTS idx_networking_source
            ON networking_activities(acquisition_source_id, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_deal_stage ON deals(stage, updated_at);
        CREATE INDEX IF NOT EXISTS idx_payment_deal ON deal_payments(deal_id, status);
        CREATE INDEX IF NOT EXISTS idx_payout_status ON partner_payouts(status, due_at);
        """
    )


class CRMStore:
    def __init__(self, path: str = "leads.db"):
        self.path = path
        with self._connect() as connection:
            ensure_crm_schema(connection)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _rows(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
        return [dict(row) for row in rows]

    def _lead_exists(self, connection: sqlite3.Connection, lead_key: str) -> None:
        if not connection.execute("SELECT 1 FROM leads WHERE lead_key = ?", (lead_key,)).fetchone():
            raise ValueError("Лид не найден.")

    def create_source(
        self,
        kind: str,
        name: str,
        *,
        platform: str = "",
        url: str = "",
        niche: str = "",
        state: str = "watching",
        activity_score: int = 0,
        audience_fit_score: int = 0,
        notes: str = "",
    ) -> int:
        _choice(kind, SOURCE_KINDS, "тип источника")
        _choice(state, SOURCE_STATES, "состояние")
        if kind == "community":
            _choice(platform, COMMUNITY_PLATFORMS, "платформа")
        elif platform:
            raise ValueError("Платформа указывается только для сообщества.")
        for score, label in ((activity_score, "активность"), (audience_fit_score, "соответствие")):
            if _non_negative_int(score, label) > 5:
                raise ValueError(f"Оценка «{label}» должна быть от 0 до 5.")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO acquisition_sources (
                    kind, platform, name, url, niche, state, activity_score, audience_fit_score, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (kind, platform, _required(name, "название"), url.strip(), niche.strip(), state,
                 activity_score, audience_fit_score, notes.strip()),
            )
            return int(cursor.lastrowid)

    def list_sources(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM acquisition_sources ORDER BY updated_at DESC, id DESC").fetchall()
        return self._rows(rows)

    def set_source_state(self, source_id: int, state: str) -> None:
        _choice(state, SOURCE_STATES, "состояние площадки")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE acquisition_sources SET state = ?, updated_at = ? WHERE id = ?",
                (state, _now(), source_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Площадка не найдена.")

    def create_positioning_profile(
        self, platform: str, audience: str, value_proposition: str, evidence: str, cta: str
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO positioning_profiles (platform, audience, value_proposition, evidence, cta)
                VALUES (?, ?, ?, ?, ?)
                """,
                (_required(platform, "платформа"), _required(audience, "аудитория"),
                 _required(value_proposition, "ценностное предложение"), evidence.strip(),
                 _required(cta, "CTA")),
            )
            return int(cursor.lastrowid)

    def approve_positioning_profile(self, profile_id: int) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT evidence FROM positioning_profiles WHERE id = ?", (profile_id,)).fetchone()
            if not row:
                raise ValueError("Профиль не найден.")
            if not str(row["evidence"]).strip():
                raise ValueError("Нельзя утвердить позиционирование без подтверждённых доказательств.")
            connection.execute(
                "UPDATE positioning_profiles SET state = 'approved', updated_at = ? WHERE id = ?",
                (_now(), profile_id),
            )

    def list_positioning_profiles(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM positioning_profiles ORDER BY updated_at DESC, id DESC").fetchall()
        return self._rows(rows)

    def create_partner(
        self,
        name: str,
        relationship_evidence: str,
        *,
        company: str = "",
        specialty: str = "",
        niches: str = "",
        email: str = "",
        telegram: str = "",
        state: str = "prospect",
        default_commission_bps: int = 1000,
        payout_delay_days: int = 3,
        notes: str = "",
    ) -> int:
        _choice(state, PARTNER_STATES, "состояние партнёра")
        commission = _non_negative_int(default_commission_bps, "комиссия")
        if commission > 10000:
            raise ValueError("Комиссия не может быть больше 100%.")
        delay = _non_negative_int(payout_delay_days, "срок выплаты")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO partners (
                    name, company, specialty, niches, email, telegram, state,
                    relationship_evidence, default_commission_bps, payout_delay_days, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (_required(name, "имя партнёра"), company.strip(), specialty.strip(), niches.strip(),
                 email.strip().lower(), telegram.strip(), state,
                 _required(relationship_evidence, "доказательство знакомства"), commission, delay,
                 notes.strip()),
            )
            return int(cursor.lastrowid)

    def list_partners(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*,
                       COUNT(DISTINCT r.id) AS referrals,
                       COUNT(DISTINCT CASE WHEN d.stage = 'won' THEN d.id END) AS won_deals
                FROM partners p
                LEFT JOIN referrals r ON r.partner_id = p.id
                LEFT JOIN deals d ON d.referral_id = r.id
                GROUP BY p.id
                ORDER BY p.updated_at DESC, p.id DESC
                """
            ).fetchall()
        return self._rows(rows)

    def set_partner_state(self, partner_id: int, state: str) -> None:
        _choice(state, PARTNER_STATES, "состояние партнёра")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE partners SET state = ?, updated_at = ? WHERE id = ?",
                (state, _now(), partner_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Партнёр не найден.")

    def add_lead_to_crm(
        self,
        lead_key: str,
        source_kind: str = "manual",
        *,
        acquisition_source_id: int | None = None,
        partner_id: int | None = None,
        referral_id: int | None = None,
        next_step: str = "",
    ) -> None:
        _choice(source_kind, SOURCE_KINDS, "источник лида")
        with self._connect() as connection:
            self._lead_exists(connection, lead_key)
            connection.execute(
                """
                INSERT INTO lead_crm_profiles (
                    lead_key, source_kind, acquisition_source_id, partner_id, referral_id, next_step
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(lead_key) DO UPDATE SET
                    source_kind = excluded.source_kind,
                    acquisition_source_id = excluded.acquisition_source_id,
                    partner_id = excluded.partner_id,
                    referral_id = excluded.referral_id,
                    next_step = excluded.next_step,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (lead_key, source_kind, acquisition_source_id, partner_id, referral_id, next_step.strip()),
            )

    def add_networking_activity(
        self,
        action_type: str,
        summary: str,
        *,
        acquisition_source_id: int | None = None,
        lead_key: str | None = None,
        partner_id: int | None = None,
        reference_url: str = "",
        outcome: str = "",
        occurred_at: str | None = None,
        next_task: str = "",
        next_task_due_at: str | None = None,
    ) -> int:
        _choice(action_type, NETWORKING_ACTIONS, "тип действия")
        if not any((acquisition_source_id, lead_key, partner_id)):
            raise ValueError("Укажите площадку, лида или партнёра.")
        with self._connect() as connection:
            if lead_key:
                self._lead_exists(connection, lead_key)
            cursor = connection.execute(
                """
                INSERT INTO networking_activities (
                    acquisition_source_id, lead_key, partner_id, action_type, reference_url,
                    summary, outcome, occurred_at, next_task, next_task_due_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (acquisition_source_id, lead_key, partner_id, action_type, reference_url.strip(),
                 _required(summary, "резюме действия"), outcome.strip(), occurred_at or _now(),
                 next_task.strip(), next_task_due_at),
            )
            return int(cursor.lastrowid)

    def list_networking_activities(self, limit: int = 200) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.*, s.name AS source_name, l.name AS lead_name, p.name AS partner_name
                FROM networking_activities a
                LEFT JOIN acquisition_sources s ON s.id = a.acquisition_source_id
                LEFT JOIN leads l ON l.lead_key = a.lead_key
                LEFT JOIN partners p ON p.id = a.partner_id
                ORDER BY a.occurred_at DESC, a.id DESC LIMIT ?
                """, (limit,)
            ).fetchall()
        return self._rows(rows)

    def create_referral(
        self,
        partner_id: int,
        lead_key: str,
        channel: str,
        evidence: str,
        *,
        introduced_at: str | None = None,
        acquisition_source_id: int | None = None,
        idempotency_key: str | None = None,
        deal_title: str = "Разработка сайта",
    ) -> tuple[int, int]:
        channel = _required(channel, "канал знакомства")
        proof = _required(evidence, "доказательство представления")
        introduced = introduced_at or _now()
        key = idempotency_key or hashlib.sha256(
            f"{partner_id}|{lead_key}|{channel}|{proof}".encode("utf-8")
        ).hexdigest()
        with self._connect() as connection:
            self._lead_exists(connection, lead_key)
            partner = connection.execute("SELECT * FROM partners WHERE id = ?", (partner_id,)).fetchone()
            if not partner:
                raise ValueError("Партнёр не найден.")
            existing = connection.execute(
                "SELECT id FROM referrals WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing:
                referral_id = int(existing["id"])
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO referrals (
                        idempotency_key, partner_id, lead_key, acquisition_source_id,
                        introduced_at, channel, evidence, commission_bps
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (key, partner_id, lead_key, acquisition_source_id, introduced, channel, proof,
                     int(partner["default_commission_bps"])),
                )
                referral_id = int(cursor.lastrowid)
        deal_id = self.create_deal(
            lead_key,
            deal_title,
            source_kind="partner",
            referral_id=referral_id,
            idempotency_key=f"referral:{referral_id}",
            create_qualify_task=True,
        )
        self.add_lead_to_crm(
            lead_key,
            "partner",
            acquisition_source_id=acquisition_source_id,
            partner_id=partner_id,
            referral_id=referral_id,
            next_step="Квалифицировать входящую рекомендацию",
        )
        return referral_id, deal_id

    def list_referrals(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT r.*, p.name AS partner_name, l.name AS lead_name, d.id AS deal_id, d.stage
                FROM referrals r
                JOIN partners p ON p.id = r.partner_id
                JOIN leads l ON l.lead_key = r.lead_key
                LEFT JOIN deals d ON d.referral_id = r.id
                ORDER BY r.introduced_at DESC, r.id DESC
                """
            ).fetchall()
        return self._rows(rows)

    def create_deal(
        self,
        lead_key: str,
        title: str,
        *,
        source_kind: str = "manual",
        referral_id: int | None = None,
        idempotency_key: str | None = None,
        create_qualify_task: bool = False,
    ) -> int:
        _choice(source_kind, SOURCE_KINDS, "источник сделки")
        clean_title = _required(title, "предмет сделки")
        title_key = " ".join(clean_title.casefold().split())
        with self._connect() as connection:
            self._lead_exists(connection, lead_key)
            if idempotency_key:
                existing = connection.execute(
                    "SELECT id FROM deals WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing:
                    return int(existing["id"])
            existing = connection.execute(
                """
                SELECT id FROM deals
                WHERE lead_key = ? AND title_key = ? AND stage NOT IN ('won','lost')
                """, (lead_key, title_key)
            ).fetchone()
            if existing:
                return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO deals (
                    lead_key, title, title_key, source_kind, referral_id, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?)
                """, (lead_key, clean_title, title_key, source_kind, referral_id, idempotency_key)
            )
            deal_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO deal_stage_history (deal_id, from_stage, to_stage, changed_at) VALUES (?, NULL, 'new', ?)",
                (deal_id, _now()),
            )
            if create_qualify_task:
                connection.execute(
                    "INSERT INTO deal_tasks (deal_id, task_type, description) VALUES (?, 'qualify', ?)",
                    (deal_id, "Квалифицировать входящий интерес"),
                )
            return deal_id

    def create_deal_from_inbox(
        self, source: str, source_id: str, lead_key: str, title: str = "Разработка сайта"
    ) -> int:
        return self.create_deal(
            lead_key,
            title,
            source_kind="inbound",
            idempotency_key=f"inbox:{source}:{source_id}",
            create_qualify_task=True,
        )

    def transition_deal(
        self,
        deal_id: int,
        to_stage: str,
        *,
        value_kopecks: int | None = None,
        reason: str = "",
    ) -> None:
        _choice(to_stage, DEAL_STAGES, "этап сделки")
        with self._connect() as connection:
            deal = connection.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
            if not deal:
                raise ValueError("Сделка не найдена.")
            current = str(deal["stage"])
            if to_stage not in DEAL_TRANSITIONS[current]:
                raise ValueError(f"Переход {current} → {to_stage} недопустим.")
            final_value = deal["value_kopecks"] if value_kopecks is None else _non_negative_int(value_kopecks, "стоимость")
            if to_stage == "won" and not final_value:
                raise ValueError("Для выигранной сделки укажите стоимость.")
            lost_reason = reason.strip() if to_stage == "lost" else ""
            if to_stage == "lost" and not lost_reason:
                raise ValueError("Для проигранной сделки укажите причину.")
            changed_at = _now()
            connection.execute(
                """
                UPDATE deals SET stage = ?, value_kopecks = ?, lost_reason = ?,
                    updated_at = ?, closed_at = ? WHERE id = ?
                """,
                (to_stage, final_value, lost_reason, changed_at,
                 changed_at if to_stage in ("won", "lost") else None, deal_id),
            )
            connection.execute(
                """
                INSERT INTO deal_stage_history (deal_id, from_stage, to_stage, reason, changed_at)
                VALUES (?, ?, ?, ?, ?)
                """, (deal_id, current, to_stage, lost_reason, changed_at)
            )
        self.reconcile_payout(deal_id)

    def list_deals(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.*, l.name AS lead_name, p.name AS partner_name,
                       COALESCE(SUM(CASE WHEN pay.status = 'paid' THEN pay.amount_kopecks ELSE 0 END), 0)
                           AS paid_kopecks
                FROM deals d
                JOIN leads l ON l.lead_key = d.lead_key
                LEFT JOIN referrals r ON r.id = d.referral_id
                LEFT JOIN partners p ON p.id = r.partner_id
                LEFT JOIN deal_payments pay ON pay.deal_id = d.id
                GROUP BY d.id
                ORDER BY d.updated_at DESC, d.id DESC
                """
            ).fetchall()
        return self._rows(rows)

    def list_stage_history(self, deal_id: int) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM deal_stage_history WHERE deal_id = ? ORDER BY id", (deal_id,)
            ).fetchall()
        return self._rows(rows)

    def add_task(self, deal_id: int, description: str, *, task_type: str = "other", due_at: str | None = None) -> int:
        with self._connect() as connection:
            if not connection.execute("SELECT 1 FROM deals WHERE id = ?", (deal_id,)).fetchone():
                raise ValueError("Сделка не найдена.")
            cursor = connection.execute(
                "INSERT INTO deal_tasks (deal_id, task_type, description, due_at) VALUES (?, ?, ?, ?)",
                (deal_id, _required(task_type, "тип задачи"), _required(description, "задача"), due_at),
            )
            return int(cursor.lastrowid)

    def set_task_status(self, task_id: int, status: str) -> None:
        _choice(status, TASK_STATES, "статус задачи")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE deal_tasks SET status = ?, completed_at = ? WHERE id = ?",
                (status, _now() if status == "done" else None, task_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Задача не найдена.")

    def list_tasks(self, deal_id: int | None = None) -> list[dict[str, object]]:
        where = "WHERE t.deal_id = ?" if deal_id is not None else ""
        params: tuple[object, ...] = (deal_id,) if deal_id is not None else ()
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT t.*, d.title, l.name AS lead_name FROM deal_tasks t
                JOIN deals d ON d.id = t.deal_id JOIN leads l ON l.lead_key = d.lead_key
                {where} ORDER BY CASE t.status WHEN 'open' THEN 0 ELSE 1 END, t.due_at, t.id DESC
                """, params
            ).fetchall()
        return self._rows(rows)

    def add_deal_note(self, deal_id: int, note_type: str, summary: str, evidence: str = "") -> int:
        _choice(note_type, NOTE_TYPES, "тип записи")
        if note_type == "verified_observation" and not evidence.strip():
            raise ValueError("Для проверяемого наблюдения сохраните доказательство.")
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO deal_notes (deal_id, note_type, summary, evidence) VALUES (?, ?, ?, ?)",
                (deal_id, note_type, _required(summary, "резюме"), evidence.strip()),
            )
            return int(cursor.lastrowid)

    def list_deal_notes(self, deal_id: int) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM deal_notes WHERE deal_id = ? ORDER BY id DESC", (deal_id,)
            ).fetchall()
        return self._rows(rows)

    def add_document(
        self, deal_id: int, document_type: str, *, number: str = "", document_date: str | None = None,
        url: str = "", status: str = "draft"
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO deal_documents (deal_id, document_type, number, document_date, url, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """, (deal_id, _required(document_type, "тип документа"), number.strip(), document_date,
                       url.strip(), _required(status, "статус документа"))
            )
            return int(cursor.lastrowid)

    def list_documents(self, deal_id: int) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM deal_documents WHERE deal_id = ? ORDER BY id DESC", (deal_id,)
            ).fetchall()
        return self._rows(rows)

    def add_payment(
        self,
        deal_id: int,
        amount_kopecks: int,
        *,
        status: str = "planned",
        due_at: str | None = None,
        paid_at: str | None = None,
        external_ref: str = "",
    ) -> int:
        _choice(status, PAYMENT_STATES, "статус платежа")
        amount = _positive_int(amount_kopecks, "сумма платежа")
        with self._connect() as connection:
            deal = connection.execute("SELECT stage FROM deals WHERE id = ?", (deal_id,)).fetchone()
            if not deal:
                raise ValueError("Сделка не найдена.")
            if status == "paid" and deal["stage"] != "won":
                raise ValueError("Оплаченный платёж можно зафиксировать только для выигранной сделки.")
            cursor = connection.execute(
                """
                INSERT INTO deal_payments (deal_id, amount_kopecks, due_at, paid_at, status, external_ref)
                VALUES (?, ?, ?, ?, ?, ?)
                """, (deal_id, amount, due_at, paid_at or (_now() if status == "paid" else None),
                       status, external_ref.strip())
            )
            payment_id = int(cursor.lastrowid)
        self.reconcile_payout(deal_id)
        return payment_id

    def set_payment_status(self, payment_id: int, status: str, *, paid_at: str | None = None) -> None:
        _choice(status, PAYMENT_STATES, "статус платежа")
        with self._connect() as connection:
            payment = connection.execute(
                """
                SELECT pay.*, d.stage FROM deal_payments pay
                JOIN deals d ON d.id = pay.deal_id WHERE pay.id = ?
                """, (payment_id,)
            ).fetchone()
            if not payment:
                raise ValueError("Платёж не найден.")
            if status == "paid" and payment["stage"] != "won":
                raise ValueError("Оплаченный платёж можно зафиксировать только для выигранной сделки.")
            connection.execute(
                """
                UPDATE deal_payments SET status = ?, paid_at = ?, updated_at = ? WHERE id = ?
                """, (status, paid_at or (_now() if status == "paid" else None), _now(), payment_id)
            )
            deal_id = int(payment["deal_id"])
        self.reconcile_payout(deal_id)

    def list_payments(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT pay.*, d.title, l.name AS lead_name FROM deal_payments pay
                JOIN deals d ON d.id = pay.deal_id JOIN leads l ON l.lead_key = d.lead_key
                ORDER BY pay.created_at DESC, pay.id DESC
                """
            ).fetchall()
        return self._rows(rows)

    def reconcile_payout(self, deal_id: int, now: str | datetime | None = None) -> dict[str, object] | None:
        moment = _as_utc(now)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT d.id AS deal_id, d.stage, d.value_kopecks, r.id AS referral_id,
                       r.partner_id, r.commission_bps, p.payout_delay_days,
                       COALESCE(SUM(CASE WHEN pay.status = 'paid' THEN pay.amount_kopecks ELSE 0 END), 0)
                           AS paid_kopecks,
                       MAX(CASE WHEN pay.status = 'paid' THEN pay.paid_at END) AS last_paid_at
                FROM deals d
                LEFT JOIN referrals r ON r.id = d.referral_id
                LEFT JOIN partners p ON p.id = r.partner_id
                LEFT JOIN deal_payments pay ON pay.deal_id = d.id
                WHERE d.id = ? GROUP BY d.id
                """, (deal_id,)
            ).fetchone()
            if not row:
                raise ValueError("Сделка не найдена.")
            if row["referral_id"] is None:
                return None
            existing = connection.execute(
                "SELECT * FROM partner_payouts WHERE referral_id = ?", (row["referral_id"],)
            ).fetchone()
            if existing and existing["status"] == "paid":
                return dict(existing)
            basis = int(row["value_kopecks"] or 0)
            if row["stage"] != "won" or basis <= 0:
                return dict(existing) if existing else None
            amount = basis * int(row["commission_bps"]) // 10000
            fully_paid = row["stage"] == "won" and basis > 0 and int(row["paid_kopecks"]) >= basis
            due_at: str | None = None
            status = "accrued"
            if fully_paid and row["last_paid_at"]:
                due_moment = _as_utc(str(row["last_paid_at"])) + timedelta(days=int(row["payout_delay_days"]))
                due_at = due_moment.isoformat()
                status = "due" if moment >= due_moment else "accrued"
            if existing:
                connection.execute(
                    """
                    UPDATE partner_payouts SET commission_bps = ?, basis_kopecks = ?, amount_kopecks = ?,
                        due_at = ?, status = ?, updated_at = ? WHERE id = ?
                    """, (int(row["commission_bps"]), basis, amount, due_at, status, moment.isoformat(),
                           existing["id"])
                )
                payout_id = int(existing["id"])
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO partner_payouts (
                        referral_id, partner_id, deal_id, commission_bps,
                        basis_kopecks, amount_kopecks, due_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (row["referral_id"], row["partner_id"], deal_id, row["commission_bps"],
                           basis, amount, due_at, status)
                )
                payout_id = int(cursor.lastrowid)
            saved = connection.execute("SELECT * FROM partner_payouts WHERE id = ?", (payout_id,)).fetchone()
            return dict(saved)

    def refresh_payouts(self, now: str | datetime | None = None) -> None:
        with self._connect() as connection:
            deal_ids = [int(row[0]) for row in connection.execute("SELECT id FROM deals WHERE referral_id IS NOT NULL")]
        for deal_id in deal_ids:
            self.reconcile_payout(deal_id, now)

    def mark_payout_paid(self, payout_id: int, paid_at: str | None = None) -> None:
        self.refresh_payouts(paid_at)
        with self._connect() as connection:
            payout = connection.execute("SELECT status FROM partner_payouts WHERE id = ?", (payout_id,)).fetchone()
            if not payout:
                raise ValueError("Выплата не найдена.")
            if payout["status"] != "due":
                raise ValueError("Оплатить можно только комиссию со статусом due.")
            connection.execute(
                "UPDATE partner_payouts SET status = 'paid', paid_at = ?, updated_at = ? WHERE id = ?",
                (paid_at or _now(), _now(), payout_id),
            )

    def list_payouts(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT po.*, p.name AS partner_name, l.name AS lead_name, d.title
                FROM partner_payouts po JOIN partners p ON p.id = po.partner_id
                JOIN deals d ON d.id = po.deal_id JOIN leads l ON l.lead_key = d.lead_key
                ORDER BY po.created_at DESC, po.id DESC
                """
            ).fetchall()
        return self._rows(rows)

    def lead_summary(self, lead_key: str) -> dict[str, object]:
        with self._connect() as connection:
            profile = connection.execute(
                """
                SELECT cp.*, s.name AS source_name, p.name AS partner_name
                FROM lead_crm_profiles cp
                LEFT JOIN acquisition_sources s ON s.id = cp.acquisition_source_id
                LEFT JOIN partners p ON p.id = cp.partner_id WHERE cp.lead_key = ?
                """, (lead_key,)
            ).fetchone()
            deal = connection.execute(
                """
                SELECT id, title, stage, value_kopecks FROM deals WHERE lead_key = ?
                AND stage NOT IN ('won','lost') ORDER BY updated_at DESC LIMIT 1
                """, (lead_key,)
            ).fetchone()
            permissions = connection.execute(
                "SELECT channel, address, status FROM contact_permissions WHERE lead_key = ? ORDER BY channel",
                (lead_key,),
            ).fetchall()
            activities = connection.execute(
                """
                SELECT action_type, summary, outcome, occurred_at FROM networking_activities
                WHERE lead_key = ? ORDER BY occurred_at DESC, id DESC LIMIT 3
                """, (lead_key,)
            ).fetchall()
        return {
            "profile": dict(profile) if profile else None,
            "active_deal": dict(deal) if deal else None,
            "permissions": self._rows(permissions),
            "activities": self._rows(activities),
        }

    def metrics(self) -> dict[str, int]:
        with self._connect() as connection:
            result = {
                "active_sources": connection.execute(
                    "SELECT COUNT(*) FROM acquisition_sources WHERE kind = 'community' AND state IN ('engaging','active')"
                ).fetchone()[0],
                "useful_comments": connection.execute(
                    "SELECT COUNT(*) FROM networking_activities WHERE action_type = 'public_comment'"
                ).fetchone()[0],
                "inbound_replies": connection.execute(
                    "SELECT COUNT(*) FROM networking_activities WHERE action_type = 'inbound_reply'"
                ).fetchone()[0],
                "active_partners": connection.execute(
                    "SELECT COUNT(*) FROM partners WHERE state = 'active'"
                ).fetchone()[0],
                "referrals": connection.execute("SELECT COUNT(*) FROM referrals").fetchone()[0],
                "qualified_deals": connection.execute(
                    "SELECT COUNT(*) FROM deals WHERE stage IN ('qualified','discovery','proposal','negotiation','won')"
                ).fetchone()[0],
                "won_kopecks": connection.execute(
                    "SELECT COALESCE(SUM(value_kopecks),0) FROM deals WHERE stage = 'won'"
                ).fetchone()[0],
                "commission_due_kopecks": connection.execute(
                    "SELECT COALESCE(SUM(amount_kopecks),0) FROM partner_payouts WHERE status = 'due'"
                ).fetchone()[0],
                "commission_paid_kopecks": connection.execute(
                    "SELECT COALESCE(SUM(amount_kopecks),0) FROM partner_payouts WHERE status = 'paid'"
                ).fetchone()[0],
            }
        return {key: int(value) for key, value in result.items()}

    def source_conversion(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_kind, COUNT(*) AS deals,
                       SUM(CASE WHEN stage = 'won' THEN 1 ELSE 0 END) AS won_deals,
                       COALESCE(SUM(CASE WHEN stage = 'won' THEN value_kopecks ELSE 0 END),0) AS won_kopecks
                FROM deals GROUP BY source_kind ORDER BY deals DESC, source_kind
                """
            ).fetchall()
        result = self._rows(rows)
        for row in result:
            row["conversion_percent"] = round(100 * int(row["won_deals"]) / int(row["deals"]), 1)
        return result

    def financial_export_csv(self) -> bytes:
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")

        def write(row: list[object]) -> None:
            writer.writerow([sanitize_export_cell(cell) for cell in row])

        writer.writerow([
            "тип", "id", "лид", "партнёр", "сделка", "статус", "сумма_коп", "дата"
        ])
        for payment in self.list_payments():
            write([
                "платёж", payment["id"], payment["lead_name"], "", payment["title"],
                payment["status"], payment["amount_kopecks"], payment["paid_at"] or payment["due_at"] or ""
            ])
        for payout in self.list_payouts():
            write([
                "комиссия", payout["id"], payout["lead_name"], payout["partner_name"], payout["title"],
                payout["status"], payout["amount_kopecks"], payout["paid_at"] or payout["due_at"] or ""
            ])
        return output.getvalue().encode("utf-8-sig")
