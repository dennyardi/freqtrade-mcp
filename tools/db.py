"""SQLite persistence layer untuk job store."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path("/opt/freqtrade-mcp/data/jobs.db")


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id       TEXT PRIMARY KEY,
                job_type     TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                progress     INTEGER NOT NULL DEFAULT 0,
                result       TEXT,
                error        TEXT,
                created_at   TEXT NOT NULL,
                finished_at  TEXT
            )
        """)


def save_job(job) -> None:
    with _conn() as con:
        con.execute("""
            INSERT INTO jobs
                (job_id, job_type, strategy_name, status, progress,
                 result, error, created_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status       = excluded.status,
                progress     = excluded.progress,
                result       = excluded.result,
                error        = excluded.error,
                finished_at  = excluded.finished_at
        """, (
            job.job_id,
            job.job_type,
            job.strategy_name,
            str(job.status),
            job.progress,
            json.dumps(job.result) if job.result is not None else None,
            job.error,
            job.created_at,
            job.finished_at,
        ))


def load_all_jobs() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC"
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        if d["result"]:
            try:
                d["result"] = json.loads(d["result"])
            except Exception:
                pass
        result.append(d)
    return result
