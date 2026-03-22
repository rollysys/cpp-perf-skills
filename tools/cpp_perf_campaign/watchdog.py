from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .config import CampaignConfig
from .util import utc_now


def _cutoff(seconds: int) -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=seconds)
    ).isoformat().replace("+00:00", "Z")


def requeue_stale_runs(connection: sqlite3.Connection, config: CampaignConfig) -> int:
    cutoff = _cutoff(config.budget.stale_after_seconds)
    stale_rows = connection.execute(
        """
        SELECT targets.id AS target_id, experiments.id AS experiment_id
        FROM targets
        LEFT JOIN experiments
          ON experiments.target_id = targets.id AND experiments.status = 'running'
        WHERE targets.status = 'running'
          AND COALESCE(targets.last_heartbeat_at, targets.last_started_at, '') < ?
        """,
        (cutoff,),
    ).fetchall()

    now = utc_now()
    for row in stale_rows:
        connection.execute(
            """
            UPDATE targets
            SET status = 'queued',
                locked_by = NULL,
                lock_expires_at = NULL,
                last_finished_at = ?
            WHERE id = ?
            """,
            (now, row["target_id"]),
        )
        if row["experiment_id"] is not None:
            connection.execute(
                """
                UPDATE experiments
                SET status = 'interrupted',
                    finished_at = ?,
                    outcome = COALESCE(outcome, 'interrupted'),
                    error_text = COALESCE(error_text, 'Requeued by watchdog after stale heartbeat')
                WHERE id = ?
                """,
                (now, row["experiment_id"]),
            )
    connection.commit()
    return len(stale_rows)
