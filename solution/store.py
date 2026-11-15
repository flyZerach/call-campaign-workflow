"""SQLite persistence for dispatcher lead and call state."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeAlias

Timestamp: TypeAlias = datetime | str | None

def _db_time(value: Timestamp) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


@dataclass(frozen=True)
class CampaignRecord:
    campaign_id: str
    tenant_id: str
    max_concurrent_calls: int


class ReservationRejected(RuntimeError):
    """A persisted guardrail prevented reserving a call intent."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class LeadRecord:
    campaign_id: str
    lead_id: str
    phone: str
    stage: str = "fresh"
    accepted_attempt_count: int = 0
    last_status: str | None = None
    last_disposition: str | None = None
    callback_at: Timestamp = None
    active_request_id: str | None = None
    active_call_id: str | None = None


@dataclass(frozen=True)
class CallRecord:
    request_id: str
    campaign_id: str
    lead_id: str
    provider_call_id: str | None = None
    status: str = "request_pending"
    disposition: str | None = None
    started_at: Timestamp = None
    ended_at: Timestamp = None
    callback_at: Timestamp = None
    accepted: bool = False
    result_applied: bool = False
    result_stage: str | None = None


class SQLiteStore:
    """Connection-per-operation SQLite store that survives process restarts."""

    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _connection(self, *, immediate: bool = False):
        db = self._connect()
        try:
            if immediate:
                db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def initialize(self) -> None:
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS campaigns (
                    campaign_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    max_concurrent_calls INTEGER NOT NULL CHECK (max_concurrent_calls > 0)
                );
                CREATE TABLE IF NOT EXISTS dnd_phones (
                    phone TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    recorded_at TEXT
                );
                CREATE TABLE IF NOT EXISTS leads (
                    campaign_id TEXT NOT NULL,
                    lead_id TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    accepted_attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_status TEXT,
                    last_disposition TEXT,
                    callback_at TEXT,
                    active_request_id TEXT,
                    active_call_id TEXT,
                    PRIMARY KEY (campaign_id, lead_id)
                );
                CREATE TABLE IF NOT EXISTS calls (
                    request_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    lead_id TEXT NOT NULL,
                    provider_call_id TEXT UNIQUE,
                    status TEXT NOT NULL,
                    disposition TEXT,
                    started_at TEXT,
                    ended_at TEXT,
                    callback_at TEXT,
                    accepted INTEGER NOT NULL DEFAULT 0,
                    result_applied INTEGER NOT NULL DEFAULT 0,
                    result_stage TEXT,
                    prior_stage TEXT,
                    FOREIGN KEY (campaign_id, lead_id)
                        REFERENCES leads(campaign_id, lead_id)
                );
                CREATE INDEX IF NOT EXISTS calls_by_lead
                    ON calls(campaign_id, lead_id, started_at);
            """)
            call_columns = {row["name"] for row in db.execute("PRAGMA table_info(calls)")}
            if "prior_stage" not in call_columns:
                db.execute("ALTER TABLE calls ADD COLUMN prior_stage TEXT")

    def register_campaign(self, campaign_id: str, tenant_id: str,
                          max_concurrent_calls: int) -> None:
        """Persist campaign-to-tenant concurrency configuration from fixtures."""
        if max_concurrent_calls < 1:
            raise ValueError("max_concurrent_calls must be positive")
        with self._connection() as db:
            db.execute("""
                INSERT INTO campaigns(campaign_id, tenant_id, max_concurrent_calls)
                VALUES (?, ?, ?)
                ON CONFLICT(campaign_id) DO UPDATE SET
                    tenant_id=excluded.tenant_id,
                    max_concurrent_calls=excluded.max_concurrent_calls
            """, (campaign_id, tenant_id, max_concurrent_calls))

    def is_phone_dnd(self, phone: str) -> bool:
        with self._connection() as db:
            return db.execute(
                "SELECT 1 FROM dnd_phones WHERE phone=?", (phone,)
            ).fetchone() is not None

    def clear(self) -> None:
        with self._connection() as db:
            db.execute("DELETE FROM calls")
            db.execute("DELETE FROM leads")
            db.execute("DELETE FROM dnd_phones")
            db.execute("DELETE FROM campaigns")

    def save_lead(self, lead: LeadRecord) -> None:
        with self._connection() as db:
            db.execute("""
                INSERT INTO leads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(campaign_id, lead_id) DO UPDATE SET
                    phone=excluded.phone, stage=excluded.stage,
                    accepted_attempt_count=excluded.accepted_attempt_count,
                    last_status=excluded.last_status,
                    last_disposition=excluded.last_disposition,
                    callback_at=excluded.callback_at,
                    active_request_id=excluded.active_request_id,
                    active_call_id=excluded.active_call_id
            """, (lead.campaign_id, lead.lead_id, lead.phone, lead.stage,
                  lead.accepted_attempt_count, lead.last_status, lead.last_disposition,
                  _db_time(lead.callback_at), lead.active_request_id, lead.active_call_id))

    def insert_lead_if_missing(self, lead: LeadRecord) -> bool:
        """Insert a fixture lead only if its campaign/lead key is new.

        Returns True when inserted. Existing progress is never overwritten;
        use save_lead for an intentional state update.
        """
        with self._connection() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO leads (
                    campaign_id, lead_id, phone, stage, accepted_attempt_count,
                    last_status, last_disposition, callback_at, active_request_id,
                    active_call_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (lead.campaign_id, lead.lead_id, lead.phone, lead.stage,
                  lead.accepted_attempt_count, lead.last_status, lead.last_disposition,
                  _db_time(lead.callback_at), lead.active_request_id, lead.active_call_id))
            return cursor.rowcount == 1

    def get_lead(self, campaign_id: str, lead_id: str) -> LeadRecord | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM leads WHERE campaign_id=? AND lead_id=?",
                             (campaign_id, lead_id)).fetchone()
        return self._lead(row) if row else None

    def list_leads(self, campaign_id: str | None = None) -> list[LeadRecord]:
        with self._connection() as db:
            if campaign_id is None:
                rows = db.execute("SELECT * FROM leads ORDER BY campaign_id, lead_id").fetchall()
            else:
                rows = db.execute("SELECT * FROM leads WHERE campaign_id=? ORDER BY lead_id",
                                  (campaign_id,)).fetchall()
        return [self._lead(row) for row in rows]

    @staticmethod
    def _lead(row: sqlite3.Row) -> LeadRecord:
        return LeadRecord(row["campaign_id"], row["lead_id"], row["phone"],
                          row["stage"], row["accepted_attempt_count"], row["last_status"],
                          row["last_disposition"], _datetime(row["callback_at"]),
                          row["active_request_id"], row["active_call_id"])

    def prepare_call(self, request_id: str, campaign_id: str, lead_id: str) -> CallRecord:
        """Persist request intent and active request ID before an external call."""
        with self._connection(immediate=True) as db:
            existing = db.execute("SELECT * FROM calls WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if (existing["campaign_id"], existing["lead_id"]) != (campaign_id, lead_id):
                    raise ValueError("request_id is already assigned to another lead")
                return self._call(existing)
            lead = db.execute("SELECT * FROM leads WHERE campaign_id=? AND lead_id=?",
                              (campaign_id, lead_id)).fetchone()
            if lead is None:
                raise KeyError(f"Unknown lead {campaign_id}/{lead_id}")
            campaign = db.execute(
                "SELECT tenant_id, max_concurrent_calls FROM campaigns WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                raise KeyError(f"Unknown campaign {campaign_id}")
            if db.execute(
                "SELECT 1 FROM dnd_phones WHERE phone=?", (lead["phone"],)
            ).fetchone():
                raise ReservationRejected("dnd_blocked")
            tenant_active = db.execute("""
                SELECT COUNT(*) FROM calls AS call
                JOIN campaigns AS campaign ON campaign.campaign_id=call.campaign_id
                WHERE campaign.tenant_id=? AND call.status IN ('request_pending', 'active')
            """, (campaign["tenant_id"],)).fetchone()[0]
            if tenant_active >= campaign["max_concurrent_calls"]:
                raise ReservationRejected("tenant_concurrency_limit")
            phone_active = db.execute("""
                SELECT 1 FROM calls AS call
                JOIN leads AS active_lead
                  ON active_lead.campaign_id=call.campaign_id AND active_lead.lead_id=call.lead_id
                WHERE active_lead.phone=? AND call.status IN ('request_pending', 'active')
                LIMIT 1
            """, (lead["phone"],)).fetchone()
            if phone_active:
                raise ReservationRejected("phone_already_active")
            if lead["active_request_id"] is not None or lead["stage"] in {"active", "in_flight"}:
                raise ReservationRejected("lead_already_active")
            db.execute("INSERT INTO calls(request_id, campaign_id, lead_id, status, prior_stage) "
                       "VALUES (?, ?, ?, 'request_pending', ?)",
                       (request_id, campaign_id, lead_id, lead["stage"]))
            db.execute("UPDATE leads SET stage='active', active_request_id=? "
                       "WHERE campaign_id=? AND lead_id=?", (request_id, campaign_id, lead_id))
            row = db.execute("SELECT * FROM calls WHERE request_id=?", (request_id,)).fetchone()
        return self._call(row)

    def mark_call_accepted(self, request_id: str, provider_call_id: str,
                           started_at: Timestamp = None) -> CallRecord:
        """Persist acceptance and increment the accepted-attempt count once."""
        with self._connection(immediate=True) as db:
            call = db.execute("SELECT * FROM calls WHERE request_id=?", (request_id,)).fetchone()
            if call is None:
                raise KeyError(f"Unknown request_id {request_id}")
            if call["provider_call_id"] not in (None, provider_call_id):
                raise ValueError("request_id already has a different provider call ID")
            if not call["accepted"]:
                db.execute("UPDATE leads SET accepted_attempt_count=accepted_attempt_count+1 "
                           "WHERE campaign_id=? AND lead_id=?",
                           (call["campaign_id"], call["lead_id"]))
            db.execute("UPDATE calls SET accepted=1, provider_call_id=?, "
                       "status=CASE WHEN result_applied=1 THEN status ELSE 'active' END, "
                       "started_at=COALESCE(started_at, ?) WHERE request_id=?",
                       (provider_call_id, _db_time(started_at), request_id))
            db.execute("UPDATE leads SET active_call_id=? WHERE campaign_id=? AND lead_id=? "
                       "AND active_request_id=?",
                       (provider_call_id, call["campaign_id"], call["lead_id"], request_id))
            row = db.execute("SELECT * FROM calls WHERE request_id=?", (request_id,)).fetchone()
        return self._call(row)

    def abandon_unaccepted_call(self, request_id: str, restore_stage: str | None = None) -> None:
        """Clear an attempt known not to have been accepted, such as an HTTP 503."""
        with self._connection() as db:
            call = db.execute("SELECT * FROM calls WHERE request_id=?", (request_id,)).fetchone()
            if call is None:
                raise KeyError(f"Unknown request_id {request_id}")
            if call["accepted"] or call["result_applied"]:
                raise ValueError("cannot abandon an accepted call")
            db.execute("UPDATE calls SET status='not_accepted' WHERE request_id=?", (request_id,))
            stage = restore_stage or call["prior_stage"] or "fresh"
            db.execute("UPDATE leads SET stage=?, active_request_id=NULL, active_call_id=NULL "
                       "WHERE campaign_id=? AND lead_id=? AND active_request_id=?",
                       (stage, call["campaign_id"], call["lead_id"], request_id))

    def get_call(self, request_id: str) -> CallRecord | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM calls WHERE request_id=?", (request_id,)).fetchone()
        return self._call(row) if row else None

    def list_calls(self, campaign_id: str | None = None,
                   lead_id: str | None = None) -> list[CallRecord]:
        filters, values = [], []
        if campaign_id is not None:
            filters.append("campaign_id=?")
            values.append(campaign_id)
        if lead_id is not None:
            filters.append("lead_id=?")
            values.append(lead_id)
        query = "SELECT * FROM calls" + (" WHERE " + " AND ".join(filters) if filters else "")
        query += " ORDER BY started_at, request_id"
        with self._connection() as db:
            rows = db.execute(query, values).fetchall()
        return [self._call(row) for row in rows]

    @staticmethod
    def _call(row: sqlite3.Row) -> CallRecord:
        return CallRecord(row["request_id"], row["campaign_id"], row["lead_id"],
                          row["provider_call_id"], row["status"], row["disposition"],
                          _datetime(row["started_at"]), _datetime(row["ended_at"]),
                          _datetime(row["callback_at"]), bool(row["accepted"]),
                          bool(row["result_applied"]), row["result_stage"])

    def apply_call_result(self, request_id: str, *, status: str,
                          disposition: str | None, ended_at: Timestamp,
                          callback_at: Timestamp = None,
                          provider_call_id: str | None = None,
                          started_at: Timestamp = None,
                          next_stage: str) -> bool:
        """Persist one result and caller-computed stage atomically. Return False for replay."""
        with self._connection(immediate=True) as db:
            call = db.execute("SELECT * FROM calls WHERE request_id=?", (request_id,)).fetchone()
            if call is None:
                raise KeyError(f"Unknown request_id {request_id}")
            if provider_call_id and call["provider_call_id"] not in (None, provider_call_id):
                raise ValueError("request_id already has a different provider call ID")
            if call["result_applied"]:
                same = (call["status"] == status and call["disposition"] == disposition
                        and call["ended_at"] == _db_time(ended_at)
                        and call["callback_at"] == _db_time(callback_at)
                        and (started_at is None or call["started_at"] == _db_time(started_at))
                        and (provider_call_id is None or call["provider_call_id"] == provider_call_id))
                if not same:
                    raise ValueError("conflicting result for an already completed call")
                return False

            accepted_now = not bool(call["accepted"])
            provider_id = provider_call_id or call["provider_call_id"]
            start_value = _db_time(started_at) or call["started_at"]
            db.execute("UPDATE calls SET provider_call_id=?, status=?, disposition=?, started_at=?, "
                       "ended_at=?, callback_at=?, accepted=1, result_applied=1, result_stage=? "
                       "WHERE request_id=?",
                       (provider_id, status, disposition, start_value, _db_time(ended_at),
                        _db_time(callback_at), next_stage, request_id))
            lead = db.execute("SELECT accepted_attempt_count FROM leads "
                              "WHERE campaign_id=? AND lead_id=?",
                              (call["campaign_id"], call["lead_id"])).fetchone()
            attempts = lead[0] + int(accepted_now)
            stage = next_stage
            db.execute("UPDATE leads SET accepted_attempt_count=?, last_status=?, "
                       "last_disposition=?, callback_at=?, stage=?, active_request_id=NULL, "
                       "active_call_id=NULL WHERE campaign_id=? AND lead_id=?",
                       (attempts, status, disposition, _db_time(callback_at), stage,
                        call["campaign_id"], call["lead_id"]))
            if disposition == "dnd":
                phone = db.execute(
                    "SELECT phone FROM leads WHERE campaign_id=? AND lead_id=?",
                    (call["campaign_id"], call["lead_id"]),
                ).fetchone()[0]
                db.execute(
                    "INSERT OR IGNORE INTO dnd_phones(phone, request_id, recorded_at) "
                    "VALUES (?, ?, ?)",
                    (phone, request_id, _db_time(ended_at)),
                )
                # DND is phone-wide. Preserve leads already completed or
                # exhausted, and block every other lead with this phone.
                db.execute(
                    "UPDATE leads SET stage='dnd_blocked', active_request_id=NULL, "
                    "active_call_id=NULL WHERE phone=? AND stage NOT IN "
                    "('completed', 'exhausted', 'dnd_blocked')",
                    (phone,),
                )
        return True
