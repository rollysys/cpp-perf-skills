from __future__ import annotations

import fnmatch
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import CampaignConfig


@dataclass(frozen=True)
class TargetSeed:
    path: str
    shard: str
    priority: float
    seed_reason: str | None


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _compute_shard(relative_path: str, shard_depth: int) -> str:
    parts = PurePosixPath(relative_path).parts[:-1]
    if not parts:
        return "."
    return "/".join(parts[:shard_depth])


def _load_frontier(path: Path | None) -> dict[str, tuple[float, str | None]]:
    if path is None:
        return {}
    frontier: dict[str, tuple[float, str | None]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            rel_path = str(entry["path"]).replace("\\", "/")
            frontier[rel_path] = (float(entry.get("priority", 1.0)), entry.get("reason"))
    return frontier


def discover_targets(
    config: CampaignConfig,
    frontier_jsonl: Path | None = None,
) -> list[TargetSeed]:
    repo_root = config.repo_root
    frontier = _load_frontier(frontier_jsonl)
    discovered: dict[str, TargetSeed] = {}

    for pattern in config.discover.include_globs:
        for match in repo_root.glob(pattern):
            if not match.is_file():
                continue
            relative = match.relative_to(repo_root).as_posix()
            if _matches_any(relative, config.discover.exclude_globs):
                continue
            priority, reason = frontier.get(relative, (1.0, None))
            discovered[relative] = TargetSeed(
                path=relative,
                shard=_compute_shard(relative, config.discover.shard_depth),
                priority=priority,
                seed_reason=reason,
            )

    for relative, (priority, reason) in frontier.items():
        candidate = repo_root / relative
        if not candidate.is_file():
            continue
        if _matches_any(relative, config.discover.exclude_globs):
            continue
        discovered.setdefault(
            relative,
            TargetSeed(
                path=relative,
                shard=_compute_shard(relative, config.discover.shard_depth),
                priority=priority,
                seed_reason=reason,
            ),
        )

    seeds = sorted(discovered.values(), key=lambda seed: (-seed.priority, seed.path))
    if config.discover.max_targets is not None:
        seeds = seeds[: config.discover.max_targets]
    return seeds


def upsert_targets(connection: sqlite3.Connection, targets: list[TargetSeed]) -> int:
    inserted = 0
    for target in targets:
        cursor = connection.execute(
            """
            INSERT INTO targets(path, shard, priority, seed_reason)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                priority = MAX(targets.priority, excluded.priority),
                seed_reason = COALESCE(excluded.seed_reason, targets.seed_reason)
            """,
            (target.path, target.shard, target.priority, target.seed_reason),
        )
        inserted += max(cursor.rowcount, 0)
    connection.commit()
    return inserted
