from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import CampaignConfig
from .util import utc_now


@dataclass(frozen=True)
class Assignment:
    target_id: int
    target_path: str
    shard: str
    attempts: int
    keep_count: int
    low_gain_streak: int
    priority: float
    strategy: str
    strategy_pass: int


@dataclass(frozen=True)
class StrategyAttemptState:
    attempts: int = 0
    last_outcome: str | None = None
    best_speedup: float | None = None


@dataclass(frozen=True)
class StrategyOption:
    name: str
    next_pass: int
    kind: str
    weight: float
    refine: bool
    forced: bool
    best_speedup: float | None


def attempted_strategies(connection: sqlite3.Connection, target_id: int) -> set[str]:
    return {
        name
        for name, state in strategy_attempt_states(connection, target_id).items()
        if state.attempts > 0
    }


def strategy_attempt_states(
    connection: sqlite3.Connection,
    target_id: int,
) -> dict[str, StrategyAttemptState]:
    rows = connection.execute(
        """
        SELECT strategy, outcome, speedup
        FROM experiments
        WHERE target_id = ? AND status IN ('finished', 'crash')
        ORDER BY id ASC
        """,
        (target_id,),
    ).fetchall()
    states: dict[str, StrategyAttemptState] = {}
    for row in rows:
        strategy = str(row["strategy"])
        previous = states.get(strategy, StrategyAttemptState())
        best_speedup = previous.best_speedup
        speedup = row["speedup"]
        if speedup is not None:
            speedup_value = float(speedup)
            if best_speedup is None or speedup_value > best_speedup:
                best_speedup = speedup_value
        states[strategy] = StrategyAttemptState(
            attempts=previous.attempts + 1,
            last_outcome=None if row["outcome"] is None else str(row["outcome"]),
            best_speedup=best_speedup,
        )
    return states


def ordered_strategies(
    config: CampaignConfig,
    keep_count: int,
    strategy_states: dict[str, StrategyAttemptState],
) -> list[StrategyOption]:
    options: list[StrategyOption] = []
    strategy_index = {strategy.name: index for index, strategy in enumerate(config.strategies)}
    kind_priority = {"exploit": 0, "explore": 1} if keep_count > 0 else {"explore": 0, "exploit": 1}

    for strategy in config.strategies:
        state = strategy_states.get(strategy.name, StrategyAttemptState())
        if state.attempts == 0:
            options.append(
                StrategyOption(
                    name=strategy.name,
                    next_pass=1,
                    kind=strategy.kind,
                    weight=strategy.weight,
                    refine=False,
                    forced=False,
                    best_speedup=state.best_speedup,
                )
            )
            continue
        if state.attempts < strategy.max_passes and state.last_outcome in {"keep", "low_gain"}:
            options.append(
                StrategyOption(
                    name=strategy.name,
                    next_pass=state.attempts + 1,
                    kind=strategy.kind,
                    weight=strategy.weight,
                    refine=True,
                    forced=False,
                    best_speedup=state.best_speedup,
                )
            )

    def sort_key(option: StrategyOption) -> tuple[float, float, float, int]:
        best_speedup = option.best_speedup if option.best_speedup is not None else 0.0
        return (
            float(kind_priority.get(option.kind, 99)),
            0.0 if option.refine else 1.0,
            -best_speedup,
            strategy_index.get(option.name, 10_000),
        )

    if options:
        return sorted(options, key=sort_key)

    fallback: list[StrategyOption] = []
    for strategy in config.strategies:
        state = strategy_states.get(strategy.name)
        if state is None or state.attempts <= 0:
            continue
        fallback.append(
            StrategyOption(
                name=strategy.name,
                next_pass=state.attempts + 1,
                kind=strategy.kind,
                weight=strategy.weight,
                refine=True,
                forced=True,
                best_speedup=state.best_speedup,
            )
        )

    def fallback_key(option: StrategyOption) -> tuple[float, float, int]:
        state = strategy_states.get(option.name, StrategyAttemptState())
        best_speedup = option.best_speedup if option.best_speedup is not None else 0.0
        successful = 0.0 if state.last_outcome in {"keep", "low_gain"} else 1.0
        return (
            successful,
            -best_speedup,
            strategy_index.get(option.name, 10_000),
        )

    return sorted(fallback, key=fallback_key)[:1]


def _target_score(row: sqlite3.Row, strategies_left: int) -> float:
    attempts = row["attempts"]
    keep_count = row["keep_count"]
    low_gain_streak = row["low_gain_streak"]
    base = row["priority"] * (1.0 + 0.35 * keep_count) * (1.0 + 0.10 * strategies_left)
    decay = (1.0 + 0.25 * attempts) * (1.0 + 0.50 * low_gain_streak)
    return base / decay


def claim_next_assignment(
    connection: sqlite3.Connection,
    config: CampaignConfig,
    worker_id: str,
) -> Assignment | None:
    now = utc_now()
    lock_expires_at = now
    connection.execute("BEGIN IMMEDIATE")
    try:
        rows = connection.execute(
            "SELECT * FROM targets WHERE status = 'queued' ORDER BY priority DESC, path ASC"
        ).fetchall()

        best_row = None
        best_strategy = None
        best_score = -1.0

        for row in rows:
            strategy_states = strategy_attempt_states(connection, row["id"])
            strategies = ordered_strategies(config, row["keep_count"], strategy_states)
            if not strategies:
                continue
            score = _target_score(row, len(strategies))
            if score > best_score:
                best_score = score
                best_row = row
                best_strategy = strategies[0]

        if best_row is None or best_strategy is None:
            connection.commit()
            return None

        result = connection.execute(
            """
            UPDATE targets
            SET status = 'running',
                locked_by = ?,
                lock_expires_at = ?,
                last_started_at = ?,
                last_heartbeat_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (worker_id, lock_expires_at, now, now, best_row["id"]),
        )
        if result.rowcount != 1:
            connection.rollback()
            return None

        connection.commit()
        return Assignment(
            target_id=best_row["id"],
            target_path=best_row["path"],
            shard=best_row["shard"],
            attempts=best_row["attempts"],
            keep_count=best_row["keep_count"],
            low_gain_streak=best_row["low_gain_streak"],
            priority=best_row["priority"],
            strategy=best_strategy.name,
            strategy_pass=best_strategy.next_pass,
        )
    except Exception:
        connection.rollback()
        raise
