import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

from lead_finder import STATUSES, Lead, WebsiteAudit
from crm import ensure_crm_schema
from outreach import ensure_outreach_schema, sync_unknown_permissions


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
                    latitude REAL,
                    longitude REAL,
                    website_source TEXT NOT NULL DEFAULT '',
                    verification_status TEXT NOT NULL DEFAULT 'ambiguous',
                    verification_evidence_json TEXT NOT NULL DEFAULT '[]',
                    need_score INTEGER NOT NULL DEFAULT 0,
                    contact_score INTEGER NOT NULL DEFAULT 0,
                    branch_count INTEGER NOT NULL DEFAULT 1,
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
            columns = {
                "latitude": "REAL",
                "longitude": "REAL",
                "website_source": "TEXT NOT NULL DEFAULT ''",
                "verification_status": "TEXT NOT NULL DEFAULT 'ambiguous'",
                "verification_evidence_json": "TEXT NOT NULL DEFAULT '[]'",
                "need_score": "INTEGER NOT NULL DEFAULT 0",
                "contact_score": "INTEGER NOT NULL DEFAULT 0",
                "branch_count": "INTEGER NOT NULL DEFAULT 1",
            }
            existing = {row[1] for row in connection.execute("PRAGMA table_info(leads)")}
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE leads ADD COLUMN {name} {definition}")
            connection.execute(
                """
                UPDATE leads
                SET verification_status = 'source_provided',
                    website_source = CASE WHEN website_source = '' THEN source ELSE website_source END
                WHERE website <> '' AND verification_status = 'ambiguous'
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS city_cache (
                    city_key TEXT PRIMARY KEY,
                    city TEXT NOT NULL,
                    south REAL NOT NULL,
                    west REAL NOT NULL,
                    north REAL NOT NULL,
                    east REAL NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS search_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    city TEXT NOT NULL,
                    preset TEXT NOT NULL,
                    osm_found INTEGER NOT NULL DEFAULT 0,
                    yandex_checked INTEGER NOT NULL DEFAULT 0,
                    sites_found INTEGER NOT NULL DEFAULT 0,
                    ready_leads INTEGER NOT NULL DEFAULT 0,
                    api_requests INTEGER NOT NULL DEFAULT 0,
                    cache_hits INTEGER NOT NULL DEFAULT 0,
                    estimated_cost REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            search_run_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(search_runs)")
            }
            if "cache_hits" not in search_run_columns:
                connection.execute(
                    "ALTER TABLE search_runs ADD COLUMN cache_hits INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS domain_verification_cache (
                    cache_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    website TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    checked_at TEXT NOT NULL
                )
                """
            )
            ensure_outreach_schema(connection)
            ensure_crm_schema(connection)
            sync_unknown_permissions(connection)

    def upsert_many(self, leads: list[Lead]) -> None:
        sql = """
            INSERT INTO leads (
                lead_key, name, category, city, address, phone, email, social,
                website, source, source_url, latitude, longitude, website_source,
                verification_status, verification_evidence_json, need_score,
                contact_score, branch_count, score, reasons_json, audit_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                website_source = excluded.website_source,
                verification_status = excluded.verification_status,
                verification_evidence_json = excluded.verification_evidence_json,
                need_score = excluded.need_score,
                contact_score = excluded.contact_score,
                branch_count = excluded.branch_count,
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
                lead.latitude,
                lead.longitude,
                lead.website_source,
                lead.verification_status,
                json.dumps(lead.verification_evidence, ensure_ascii=False),
                lead.need_score,
                lead.contact_score,
                lead.branch_count,
                lead.score,
                json.dumps(lead.reasons, ensure_ascii=False),
                json.dumps(asdict(lead.audit), ensure_ascii=False) if lead.audit else None,
            )
            for lead in leads
        ]
        with self._connect() as connection:
            connection.executemany(sql, rows)
            sync_unknown_permissions(connection, [lead.lead_key for lead in leads])

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

    @staticmethod
    def _city_key(city: str) -> str:
        return " ".join(city.lower().split())

    def save_city_bbox(self, city: str, bbox: tuple[float, float, float, float]) -> None:
        south, west, north, east = bbox
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO city_cache (city_key, city, south, west, north, east, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(city_key) DO UPDATE SET
                    city = excluded.city, south = excluded.south, west = excluded.west,
                    north = excluded.north, east = excluded.east, updated_at = excluded.updated_at
                """,
                (self._city_key(city), city.strip(), south, west, north, east, datetime.now(timezone.utc).isoformat()),
            )

    def get_city_bbox(self, city: str, max_age_days: int = 30) -> tuple[float, float, float, float] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT south, west, north, east, updated_at FROM city_cache WHERE city_key = ?",
                (self._city_key(city),),
            ).fetchone()
        if not row:
            return None
        updated_at = datetime.fromisoformat(row["updated_at"])
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        if updated_at < datetime.now(timezone.utc) - timedelta(days=max_age_days):
            return None
        return row["south"], row["west"], row["north"], row["east"]

    def get_domain_verification(
        self,
        cache_key: str,
        max_age_days: int = 90,
    ) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT status, website, evidence_json, checked_at
                FROM domain_verification_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
        if not row:
            return None
        checked_at = datetime.fromisoformat(row["checked_at"])
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        if checked_at < datetime.now(timezone.utc) - timedelta(days=max_age_days):
            return None
        return {
            "status": row["status"],
            "website": row["website"],
            "evidence": json.loads(row["evidence_json"]),
            "checked_at": row["checked_at"],
        }

    def save_domain_verification(
        self,
        cache_key: str,
        status: str,
        website: str,
        evidence: list[str],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO domain_verification_cache (
                    cache_key, status, website, evidence_json, checked_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    status = excluded.status,
                    website = excluded.website,
                    evidence_json = excluded.evidence_json,
                    checked_at = excluded.checked_at
                """,
                (
                    cache_key,
                    status,
                    website,
                    json.dumps(evidence, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def record_search_run(
        self,
        city: str,
        preset: str,
        osm_found: int,
        yandex_checked: int,
        sites_found: int,
        ready_leads: int,
        api_requests: int,
        estimated_cost: float,
        cache_hits: int = 0,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO search_runs (
                    city, preset, osm_found, yandex_checked, sites_found,
                    ready_leads, api_requests, cache_hits, estimated_cost
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    city,
                    preset,
                    osm_found,
                    yandex_checked,
                    sites_found,
                    ready_leads,
                    api_requests,
                    cache_hits,
                    estimated_cost,
                ),
            )

    def list_search_runs(self, limit: int = 10) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM search_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def monthly_yandex_requests(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(api_requests), 0) AS total
                FROM search_runs
                WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
                """
            ).fetchone()
        return int(row["total"])
