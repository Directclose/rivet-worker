from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  source_url TEXT NOT NULL,
  allowed_host TEXT NOT NULL,
  ingest_authorized INTEGER NOT NULL CHECK (ingest_authorized IN (0,1)),
  republish_authorized INTEGER NOT NULL CHECK (republish_authorized IN (0,1)),
  rights_note TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  source_url TEXT NOT NULL,
  captions_srt TEXT,
  status TEXT NOT NULL,
  error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS clips (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id),
  source_id TEXT NOT NULL REFERENCES sources(id),
  start_seconds REAL NOT NULL,
  end_seconds REAL NOT NULL,
  score REAL NOT NULL,
  output_path TEXT NOT NULL,
  status TEXT NOT NULL,
  review_note TEXT,
  reviewed_at REAL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS destinations (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  display_name TEXT NOT NULL,
  enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
  config_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS publishing_outbox (
  id TEXT PRIMARY KEY,
  clip_id TEXT NOT NULL REFERENCES clips(id),
  destination_id TEXT NOT NULL REFERENCES destinations(id),
  status TEXT NOT NULL,
  approval_note TEXT NOT NULL,
  external_id TEXT,
  error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  details_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS creators (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  priority TEXT NOT NULL,
  ingest_authorized INTEGER NOT NULL CHECK (ingest_authorized IN (0,1)),
  republish_authorized INTEGER NOT NULL CHECK (republish_authorized IN (0,1)),
  rights_note TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS creator_channels (
  id TEXT PRIMARY KEY,
  creator_id TEXT NOT NULL REFERENCES creators(id),
  platform TEXT NOT NULL CHECK (platform IN ('twitch','youtube','kick')),
  platform_key TEXT NOT NULL,
  channel_url TEXT NOT NULL,
  enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
  last_checked_at REAL,
  last_error TEXT,
  created_at REAL NOT NULL,
  UNIQUE(platform, platform_key)
);
CREATE TABLE IF NOT EXISTS source_candidates (
  id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL REFERENCES creator_channels(id),
  platform TEXT NOT NULL,
  external_id TEXT NOT NULL,
  title TEXT NOT NULL,
  page_url TEXT NOT NULL,
  media_url TEXT,
  state TEXT NOT NULL,
  started_at TEXT,
  viewer_count INTEGER,
  raw_json TEXT NOT NULL,
  first_seen_at REAL NOT NULL,
  last_seen_at REAL NOT NULL,
  UNIQUE(platform, external_id)
);
CREATE INDEX IF NOT EXISTS candidates_state_seen ON source_candidates(state, last_seen_at);
CREATE TABLE IF NOT EXISTS platform_accounts (
  id TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  handle TEXT NOT NULL,
  health_status TEXT NOT NULL,
  monetization_status TEXT NOT NULL,
  monetization_json TEXT NOT NULL,
  last_synced_at REAL,
  created_at REAL NOT NULL,
  UNIQUE(platform, handle)
);
CREATE TABLE IF NOT EXISTS safety_incidents (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES platform_accounts(id),
  kind TEXT NOT NULL,
  severity TEXT NOT NULL,
  status TEXT NOT NULL,
  title TEXT NOT NULL,
  evidence_url TEXT,
  occurred_at REAL NOT NULL,
  expires_at REAL,
  appeal_deadline REAL,
  details_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS safety_status_deadline ON safety_incidents(status, appeal_deadline);
"""

CORE_CREATORS = (
    ("kai-cenat", "Kai Cenat", "S"), ("duke-dennis", "Duke Dennis", "S"),
    ("agent-00", "Agent 00", "S"), ("reggie", "Reggie", "S"),
    ("rakai", "RaKai", "S"), ("reemknocks", "Reemknocks", "A"),
    ("madi", "Madi", "A"), ("cuffem", "Cuffem", "A"),
    ("plaqueboymax", "PlaqueBoyMax", "S"), ("raud", "Raud", "A"),
    ("silky", "Silky", "A"), ("dezz", "Dezz", "A"),
    ("tylil", "Tylil", "S"), ("fanum", "Fanum", "A"),
    ("neon", "Neon", "A"), ("kaleel", "Kaleel", "B"),
    ("clover-boys", "Clover Boys", "B"), ("marlon", "Marlon", "B"),
    ("jason-the-ween", "JasonTheWeen", "B"),
)


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
        self.seed_core_creators()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def audit(self, event_type: str, entity_id: str, details: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO audit_events(event_type,entity_id,details_json,created_at) VALUES(?,?,?,?)",
                (event_type, entity_id, json.dumps(details, sort_keys=True), time.time()),
            )

    def list_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = self._rows(db.execute(
                "SELECT * FROM audit_events ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall())
        for row in rows:
            row["details"] = json.loads(row.pop("details_json"))
        return rows

    def create_platform_account(self, data: dict[str, Any]) -> dict[str, Any]:
        account_id, now = data.get("id") or str(uuid.uuid4()), time.time()
        with self.connect() as db:
            db.execute(
                """INSERT INTO platform_accounts
                (id,platform,handle,health_status,monetization_status,monetization_json,last_synced_at,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (account_id, data["platform"], data["handle"], data.get("health_status", "unknown"),
                 data.get("monetization_status", "unknown"), json.dumps(data.get("monetization", {}), sort_keys=True),
                 data.get("last_synced_at"), now),
            )
        self.audit("platform_account.created", account_id, {"platform": data["platform"], "handle": data["handle"]})
        return self.get_platform_account(account_id)

    def get_platform_account(self, account_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM platform_accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise KeyError(account_id)
        result = dict(row)
        result["monetization"] = json.loads(result.pop("monetization_json"))
        return result

    def list_platform_accounts(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM platform_accounts ORDER BY created_at").fetchall()]
        return [self.get_platform_account(item) for item in ids]

    def create_safety_incident(self, data: dict[str, Any]) -> dict[str, Any]:
        incident_id, now = data.get("id") or str(uuid.uuid4()), time.time()
        self.get_platform_account(data["account_id"])
        with self.connect() as db:
            db.execute(
                """INSERT INTO safety_incidents
                (id,account_id,kind,severity,status,title,evidence_url,occurred_at,expires_at,appeal_deadline,details_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (incident_id, data["account_id"], data["kind"], data["severity"], data.get("status", "open"),
                 data["title"], data.get("evidence_url"), data["occurred_at"], data.get("expires_at"),
                 data.get("appeal_deadline"), json.dumps(data.get("details", {}), sort_keys=True), now),
            )
        self.audit("safety_incident.created", incident_id, {"kind": data["kind"], "severity": data["severity"]})
        return self.get_safety_incident(incident_id)

    def get_safety_incident(self, incident_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM safety_incidents WHERE id=?", (incident_id,)).fetchone()
        if row is None:
            raise KeyError(incident_id)
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json"))
        return result

    def list_safety_incidents(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = self._rows(db.execute(
                """SELECT i.*,a.platform,a.handle FROM safety_incidents i
                JOIN platform_accounts a ON a.id=i.account_id ORDER BY i.occurred_at DESC"""
            ).fetchall())
        for row in rows:
            row["details"] = json.loads(row.pop("details_json"))
        return rows

    def seed_core_creators(self) -> None:
        now = time.time()
        with self.connect() as db:
            for creator_id, name, priority in CORE_CREATORS:
                db.execute(
                    """INSERT OR IGNORE INTO creators
                    (id,display_name,priority,ingest_authorized,republish_authorized,rights_note,created_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (creator_id, name, priority, 1, 0, "Owner stated clipping permission in Build Rivet Business (7)", now),
                )

    def list_creators(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return self._rows(db.execute(
                "SELECT * FROM creators ORDER BY CASE priority WHEN 'S' THEN 0 WHEN 'A' THEN 1 ELSE 2 END, display_name"
            ).fetchall())

    def create_channel(self, data: dict[str, Any]) -> dict[str, Any]:
        channel_id, now = data.get("id") or str(uuid.uuid4()), time.time()
        with self.connect() as db:
            db.execute(
                """INSERT INTO creator_channels
                (id,creator_id,platform,platform_key,channel_url,enabled,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (channel_id, data["creator_id"], data["platform"], data["platform_key"],
                 data["channel_url"], int(bool(data.get("enabled", True))), now),
            )
        self.audit("channel.created", channel_id, {"platform": data["platform"], "creator_id": data["creator_id"]})
        return self.get_channel(channel_id)

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM creator_channels WHERE id=?", (channel_id,)).fetchone()
        if row is None:
            raise KeyError(channel_id)
        return dict(row)

    def list_channels(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE c.enabled=1" if enabled_only else ""
        with self.connect() as db:
            return self._rows(db.execute(
                f"""SELECT c.*, r.display_name AS creator_name, r.priority,
                r.ingest_authorized, r.republish_authorized
                FROM creator_channels c JOIN creators r ON r.id=c.creator_id
                {where} ORDER BY r.display_name,c.platform"""
            ).fetchall())

    def mark_channel_checked(self, channel_id: str, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE creator_channels SET last_checked_at=?,last_error=? WHERE id=?",
                (time.time(), error, channel_id),
            )

    def upsert_candidate(self, channel_id: str, item: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        candidate_id = str(uuid.uuid4())
        with self.connect() as db:
            existing = db.execute(
                "SELECT id FROM source_candidates WHERE platform=? AND external_id=?",
                (item["platform"], item["external_id"]),
            ).fetchone()
            db.execute(
                """INSERT INTO source_candidates
                (id,channel_id,platform,external_id,title,page_url,media_url,state,started_at,viewer_count,raw_json,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(platform,external_id) DO UPDATE SET
                  title=excluded.title,page_url=excluded.page_url,media_url=excluded.media_url,
                  state=excluded.state,started_at=excluded.started_at,viewer_count=excluded.viewer_count,
                  raw_json=excluded.raw_json,last_seen_at=excluded.last_seen_at""",
                (candidate_id, channel_id, item["platform"], item["external_id"], item.get("title", ""),
                 item["page_url"], item.get("media_url"), item["state"], item.get("started_at"),
                 item.get("viewer_count"), json.dumps(item.get("raw", {}), sort_keys=True), now, now),
            )
            row = db.execute(
                "SELECT * FROM source_candidates WHERE platform=? AND external_id=?",
                (item["platform"], item["external_id"]),
            ).fetchone()
        result = dict(row)
        if existing is None:
            self.audit("candidate.observed", result["id"], {"platform": item["platform"], "state": item["state"]})
        return result

    def close_unseen_live_candidates(self, platform: str, seen_external_ids: set[str]) -> None:
        with self.connect() as db:
            if seen_external_ids:
                placeholders = ",".join("?" for _ in seen_external_ids)
                db.execute(
                    f"""UPDATE source_candidates SET state='ended',last_seen_at=?
                    WHERE platform=? AND state='live' AND external_id NOT IN ({placeholders})""",
                    (time.time(), platform, *sorted(seen_external_ids)),
                )
            else:
                db.execute(
                    "UPDATE source_candidates SET state='ended',last_seen_at=? WHERE platform=? AND state='live'",
                    (time.time(), platform),
                )

    def list_candidates(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return self._rows(db.execute(
                """SELECT s.*, c.creator_id, r.display_name AS creator_name
                FROM source_candidates s JOIN creator_channels c ON c.id=s.channel_id
                JOIN creators r ON r.id=c.creator_id ORDER BY s.last_seen_at DESC LIMIT 500"""
            ).fetchall())

    def create_source(self, data: dict[str, Any]) -> dict[str, Any]:
        source_id = data.get("id") or str(uuid.uuid4())
        now = time.time()
        with self.connect() as db:
            db.execute(
                """INSERT INTO sources
                (id,name,source_url,allowed_host,ingest_authorized,republish_authorized,rights_note,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    source_id, data["name"], data["source_url"], data["allowed_host"],
                    int(bool(data.get("ingest_authorized"))), int(bool(data.get("republish_authorized"))),
                    data["rights_note"], now,
                ),
            )
        self.audit("source.created", source_id, {"authorized": bool(data.get("ingest_authorized"))})
        return self.get_source(source_id)

    def get_source(self, source_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise KeyError(source_id)
        return dict(row)

    def list_sources(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return self._rows(db.execute("SELECT * FROM sources ORDER BY created_at DESC").fetchall())

    def create_job(self, source_id: str, source_url: str, captions_srt: str | None) -> dict[str, Any]:
        job_id, now = str(uuid.uuid4()), time.time()
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs(id,source_id,source_url,captions_srt,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (job_id, source_id, source_url, captions_srt, "queued", now, now),
            )
        self.audit("job.queued", job_id, {"source_id": source_id})
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return self._rows(db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200").fetchall())

    def claim_job(self) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                db.execute("COMMIT")
                return None
            changed = db.execute(
                "UPDATE jobs SET status='processing',updated_at=? WHERE id=? AND status='queued'",
                (time.time(), row["id"]),
            ).rowcount
            db.execute("COMMIT")
        return self.get_job(row["id"]) if changed else None

    def recover_interrupted_jobs(self) -> list[str]:
        """Requeue work left in-flight by a process restart.

        Any clip rows written before the interruption are removed so retrying is
        idempotent. The worker overwrites deterministic artifact filenames.
        """
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job_ids = [row[0] for row in db.execute(
                "SELECT id FROM jobs WHERE status='processing' ORDER BY created_at"
            ).fetchall()]
            for job_id in job_ids:
                db.execute("DELETE FROM clips WHERE job_id=?", (job_id,))
                db.execute(
                    "UPDATE jobs SET status='queued',error=?,updated_at=? WHERE id=?",
                    ("requeued after worker restart", now, job_id),
                )
            db.execute("COMMIT")
        for job_id in job_ids:
            self.audit("job.recovered", job_id, {"reason": "worker restart"})
        return job_ids

    def set_job_status(self, job_id: str, status: str, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?", (status, error, time.time(), job_id))
        self.audit(f"job.{status}", job_id, {"error": error} if error else {})

    def create_clip(self, job: dict[str, Any], start: float, end: float, score: float, output_path: str) -> dict[str, Any]:
        clip_id, now = str(uuid.uuid4()), time.time()
        with self.connect() as db:
            db.execute(
                """INSERT INTO clips
                (id,job_id,source_id,start_seconds,end_seconds,score,output_path,status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (clip_id, job["id"], job["source_id"], start, end, score, output_path, "pending_review", now),
            )
        self.audit("clip.created", clip_id, {"job_id": job["id"], "score": score})
        return self.get_clip(clip_id)

    def get_clip(self, clip_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        if row is None:
            raise KeyError(clip_id)
        return dict(row)

    def list_clips(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return self._rows(db.execute("SELECT * FROM clips ORDER BY created_at DESC LIMIT 200").fetchall())

    def review_clip(self, clip_id: str, decision: str, note: str) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        with self.connect() as db:
            changed = db.execute(
                "UPDATE clips SET status=?,review_note=?,reviewed_at=? WHERE id=? AND status='pending_review'",
                (decision, note, time.time(), clip_id),
            ).rowcount
        if not changed:
            raise ValueError("clip is missing or has already been reviewed")
        self.audit(f"clip.{decision}", clip_id, {"note": note})
        return self.get_clip(clip_id)

    def create_destination(self, data: dict[str, Any]) -> dict[str, Any]:
        destination_id, now = data.get("id") or str(uuid.uuid4()), time.time()
        with self.connect() as db:
            db.execute(
                "INSERT INTO destinations(id,kind,display_name,enabled,config_json,created_at) VALUES(?,?,?,?,?,?)",
                (destination_id, data["kind"], data["display_name"], int(bool(data.get("enabled"))), json.dumps(data.get("config", {})), now),
            )
        self.audit("destination.created", destination_id, {"kind": data["kind"], "enabled": bool(data.get("enabled"))})
        return self.get_destination(destination_id)

    def get_destination(self, destination_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM destinations WHERE id=?", (destination_id,)).fetchone()
        if row is None:
            raise KeyError(destination_id)
        result = dict(row)
        result["config"] = json.loads(result.pop("config_json"))
        return result

    def list_destinations(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM destinations ORDER BY created_at DESC").fetchall()]
        return [self.get_destination(item) for item in ids]

    def queue_publish(self, clip_id: str, destination_id: str, approval_note: str) -> dict[str, Any]:
        clip = self.get_clip(clip_id)
        source = self.get_source(clip["source_id"])
        destination = self.get_destination(destination_id)
        if clip["status"] != "approved":
            raise ValueError("clip must be approved before publishing")
        if not source["republish_authorized"]:
            raise PermissionError("source lacks republishing authorization")
        if not destination["enabled"]:
            raise PermissionError("destination is disabled")
        outbox_id, now = str(uuid.uuid4()), time.time()
        with self.connect() as db:
            db.execute(
                """INSERT INTO publishing_outbox
                (id,clip_id,destination_id,status,approval_note,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)""",
                (outbox_id, clip_id, destination_id, "staged", approval_note, now, now),
            )
        self.audit("publish.staged", outbox_id, {"clip_id": clip_id, "destination_id": destination_id})
        return self.get_publish(outbox_id)

    def get_publish(self, outbox_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM publishing_outbox WHERE id=?", (outbox_id,)).fetchone()
        if row is None:
            raise KeyError(outbox_id)
        return dict(row)

    def list_publishes(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return self._rows(db.execute("SELECT * FROM publishing_outbox ORDER BY created_at DESC LIMIT 200").fetchall())

    def claim_publish(self) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM publishing_outbox WHERE status='staged' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                db.execute("COMMIT")
                return None
            changed = db.execute(
                "UPDATE publishing_outbox SET status='dispatching',updated_at=? WHERE id=? AND status='staged'",
                (time.time(), row["id"]),
            ).rowcount
            db.execute("COMMIT")
        return self.get_publish(row["id"]) if changed else None

    def set_publish_status(self, outbox_id: str, status: str, *, external_id: str | None = None, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE publishing_outbox SET status=?,external_id=?,error=?,updated_at=? WHERE id=?",
                (status, external_id, error, time.time(), outbox_id),
            )
        self.audit(f"publish.{status}", outbox_id, {"external_id": external_id, "error": error})

    def counts(self) -> dict[str, int]:
        with self.connect() as db:
            return {
                "jobs": db.execute("SELECT count(*) FROM jobs").fetchone()[0],
                "clips": db.execute("SELECT count(*) FROM clips").fetchone()[0],
                "publishing_outbox": db.execute("SELECT count(*) FROM publishing_outbox").fetchone()[0],
                "queued_jobs": db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0],
                "creators": db.execute("SELECT count(*) FROM creators").fetchone()[0],
                "channels": db.execute("SELECT count(*) FROM creator_channels WHERE enabled=1").fetchone()[0],
                "live_candidates": db.execute("SELECT count(*) FROM source_candidates WHERE state='live'").fetchone()[0],
            }
