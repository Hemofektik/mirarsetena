"""Durable job queue on SQLite: pending -> running -> done, with retries.

Single-worker semantics (the poller drains the queue inline). Enqueue is
idempotent per dedupe key, so scheduled re-enqueues are free.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

MAX_ATTEMPTS = 3

Handler = Callable[[dict], None]


class JobQueue:
    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created REAL NOT NULL,
                updated REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    def enqueue(self, job_type: str, payload: dict, *, dedupe: str | None = None) -> bool:
        """Add a job; returns False when the dedupe key already exists."""
        key = dedupe or f"{job_type}:{uuid.uuid4().hex}"
        now = time.time()
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO jobs (dedupe, type, payload, status, attempts, created, updated) "
            "VALUES (?, ?, ?, 'pending', 0, ?, ?)",
            (key, job_type, json.dumps(payload), now, now),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def pending(self, types: Sequence[str] | None = None) -> dict | None:
        """Next pending job, optionally restricted to handler-registered
        types (jobs whose handler isn't registered stay parked)."""
        if types is not None and not types:
            return None
        sql = "SELECT * FROM jobs WHERE status = 'pending'"
        params: tuple = ()
        if types is not None:
            placeholders = ",".join("?" for _ in types)
            sql += f" AND type IN ({placeholders})"
            params = tuple(types)
        sql += " ORDER BY id LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def last_job(self, job_type: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE type = ? ORDER BY id DESC LIMIT 1",
            (job_type,),
        ).fetchone()
        return dict(row) if row else None

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def run_once(self, handlers: dict[str, Handler]) -> str | None:
        """Run the next pending job of a registered type; returns its
        resulting status (None when nothing runnable remains — jobs whose
        type has no handler are parked, not failed)."""
        job = self.pending(list(handlers))
        if job is None:
            return None

        self._conn.execute(
            "UPDATE jobs SET status = 'running', updated = ? WHERE id = ?",
            (time.time(), job["id"]),
        )
        self._conn.commit()

        handler = handlers[job["type"]]

        try:
            handler(json.loads(job["payload"]))
        except Exception as exc:  # noqa: BLE001 - the queue owns job failures
            attempts = job["attempts"] + 1
            status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            return self._finish(job, status=status, error=str(exc), attempts=attempts)
        return self._finish(job, status="done")

    def _finish(
        self, job: dict, *, status: str, error: str | None = None,
        attempts: int | None = None,
    ) -> str:
        self._conn.execute(
            "UPDATE jobs SET status = ?, attempts = ?, last_error = ?, updated = ? "
            "WHERE id = ?",
            (
                status,
                job["attempts"] if attempts is None else attempts,
                error if error is not None else job["last_error"],
                time.time(),
                job["id"],
            ),
        )
        self._conn.commit()
        return status
