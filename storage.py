import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from collections.abc import Iterator

from lead_finder import STATUSES, Lead, WebsiteAudit


class LeadStore:
    def __init__(self, path: str = "leads.db"):
        self.path = path
        self._create_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _create_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    lead_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '',
                    city TEXT NOT NULL DEFAULT '',
                    address TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    social TEXT NOT NULL DEFAULT '',
                    website TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    score INTEGER NOT NULL DEFAULT 0,
                    reasons_json TEXT NOT NULL DEFAULT '[]',
                    audit_json TEXT,
                    status TEXT NOT NULL DEFAULT 'Новый',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def upsert_many(self, leads: list[Lead]) -> None:
        sql = """
            INSERT INTO leads (
                lead_key, name, category, city, address, phone, email, social,
                website, source, source_url, score, reasons_json, audit_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lead_key) DO UPDATE SET
                name = excluded.name,
                category = excluded.category,
                city = excluded.city,
                address = excluded.address,
                phone = excluded.phone,
                email = excluded.email,
                social = excluded.social,
                website = excluded.website,
                source = excluded.source,
                source_url = excluded.source_url,
                score = excluded.score,
                reasons_json = excluded.reasons_json,
                audit_json = excluded.audit_json,
                updated_at = CURRENT_TIMESTAMP
        """
        rows = [
            (
                lead.lead_key,
                lead.name,
                lead.category,
                lead.city,
                lead.address,
                lead.phone,
                lead.email,
                lead.social,
                lead.website,
                lead.source,
                lead.source_url,
                lead.score,
                json.dumps(lead.reasons, ensure_ascii=False),
                json.dumps(asdict(lead.audit), ensure_ascii=False) if lead.audit else None,
            )
            for lead in leads
        ]
        with self._connect() as connection:
            connection.executemany(sql, rows)

    def list_leads(self) -> list[Lead]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM leads ORDER BY score DESC, name").fetchall()
        result: list[Lead] = []
        for row in rows:
            audit_data = json.loads(row["audit_json"]) if row["audit_json"] else None
            result.append(
                Lead(
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
                    score=row["score"],
                    reasons=json.loads(row["reasons_json"]),
                    status=row["status"],
                    note=row["note"],
                    audit=WebsiteAudit(**audit_data) if audit_data else None,
                )
            )
        return result

    def update_status(self, lead_key: str, status: str) -> None:
        if status not in STATUSES:
            raise ValueError("Неизвестный статус лида.")
        with self._connect() as connection:
            connection.execute(
                "UPDATE leads SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE lead_key = ?",
                (status, lead_key),
            )

    def update_note(self, lead_key: str, note: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE leads SET note = ?, updated_at = CURRENT_TIMESTAMP WHERE lead_key = ?",
                (note.strip(), lead_key),
            )
