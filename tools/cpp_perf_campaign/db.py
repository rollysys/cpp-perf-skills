from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import CampaignConfig
from .util import ensure_dir, utc_now


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    shard TEXT NOT NULL,
    priority REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    keep_count INTEGER NOT NULL DEFAULT 0,
    low_gain_streak INTEGER NOT NULL DEFAULT 0,
    best_speedup REAL NOT NULL DEFAULT 1.0,
    best_case_dir TEXT,
    last_strategy TEXT,
    last_speedup REAL,
    seed_reason TEXT,
    locked_by TEXT,
    lock_expires_at TEXT,
    last_heartbeat_at TEXT,
    last_started_at TEXT,
    last_finished_at TEXT
);

CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    status TEXT NOT NULL,
    case_dir TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    last_heartbeat_at TEXT,
    baseline_median_ns REAL,
    optimized_median_ns REAL,
    speedup REAL,
    outcome TEXT,
    notes TEXT,
    error_text TEXT,
    FOREIGN KEY(target_id) REFERENCES targets(id)
);

CREATE INDEX IF NOT EXISTS idx_targets_status ON targets(status);
CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(status);
CREATE INDEX IF NOT EXISTS idx_experiments_target ON experiments(target_id);
"""


def open_db(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(config: CampaignConfig) -> None:
    ensure_dir(config.runtime_root)
    ensure_dir(config.cases_root)
    with open_db(config.db_path) as connection:
        connection.executescript(SCHEMA)
        snapshot = json.dumps(config.snapshot_payload(), sort_keys=True)
        now = utc_now()
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('config_snapshot', ?)",
            (snapshot,),
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('initialized_at', ?)",
            (now,),
        )
        connection.commit()
