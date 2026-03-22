from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


DEFAULT_INCLUDE_GLOBS = (
    "**/*.cc",
    "**/*.cpp",
    "**/*.cxx",
    "**/*.h",
    "**/*.hh",
    "**/*.hpp",
    "**/*.hxx",
)

DEFAULT_EXCLUDE_GLOBS = (
    ".git/**",
    ".cpp-perf/**",
    "build/**",
    "cmake-build*/**",
    "out/**",
    "dist/**",
    "third_party/**",
    "vendor/**",
    "reference/**",
)

REQUIRED_HOOKS = ("prepare_case", "baseline", "optimize", "benchmark")


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    kind: str = "explore"
    weight: float = 1.0
    max_passes: int = 1


@dataclass(frozen=True)
class DiscoverConfig:
    include_globs: tuple[str, ...]
    exclude_globs: tuple[str, ...]
    shard_depth: int
    max_targets: int | None


@dataclass(frozen=True)
class BudgetConfig:
    max_attempts_per_target: int
    max_low_gain_streak: int
    stale_after_seconds: int
    heartbeat_interval_seconds: int


@dataclass(frozen=True)
class SelectionConfig:
    keep_min_speedup: float
    low_gain_speedup: float


@dataclass(frozen=True)
class CampaignConfig:
    config_path: Path
    campaign_id: str
    repo_root: Path
    runtime_root: Path
    discover: DiscoverConfig
    budget: BudgetConfig
    selection: SelectionConfig
    strategies: tuple[StrategyConfig, ...]
    hooks: dict[str, tuple[str, ...]]

    @property
    def db_path(self) -> Path:
        return self.runtime_root / "state.db"

    @property
    def cases_root(self) -> Path:
        return self.runtime_root / "cases"

    @property
    def stop_file(self) -> Path:
        return self.runtime_root / "STOP"

    @property
    def heartbeat_path(self) -> Path:
        return self.runtime_root / "heartbeat.json"

    def snapshot_payload(self) -> dict[str, object]:
        return {
            "campaign_id": self.campaign_id,
            "repo_root": str(self.repo_root),
            "runtime_root": str(self.runtime_root),
            "discover": {
                "include_globs": list(self.discover.include_globs),
                "exclude_globs": list(self.discover.exclude_globs),
                "shard_depth": self.discover.shard_depth,
                "max_targets": self.discover.max_targets,
            },
            "budget": {
                "max_attempts_per_target": self.budget.max_attempts_per_target,
                "max_low_gain_streak": self.budget.max_low_gain_streak,
                "stale_after_seconds": self.budget.stale_after_seconds,
                "heartbeat_interval_seconds": self.budget.heartbeat_interval_seconds,
            },
            "selection": {
                "keep_min_speedup": self.selection.keep_min_speedup,
                "low_gain_speedup": self.selection.low_gain_speedup,
            },
            "strategies": [
                {
                    "name": strategy.name,
                    "kind": strategy.kind,
                    "weight": strategy.weight,
                    "max_passes": strategy.max_passes,
                }
                for strategy in self.strategies
            ],
            "hooks": {name: list(command) for name, command in self.hooks.items()},
        }


def _resolve_path(base_dir: Path, raw_path: str | None, fallback: Path) -> Path:
    if raw_path is None:
        return fallback.resolve()
    path = Path(raw_path)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def _load_hooks(raw_hooks: dict[str, object]) -> dict[str, tuple[str, ...]]:
    hooks: dict[str, tuple[str, ...]] = {}
    for hook_name in REQUIRED_HOOKS:
        raw_command = raw_hooks.get(hook_name)
        if not isinstance(raw_command, list) or not raw_command or not all(
            isinstance(part, str) and part for part in raw_command
        ):
            raise ValueError(f"Hook '{hook_name}' must be a non-empty list of command arguments")
        hooks[hook_name] = tuple(raw_command)
    return hooks


def load_config(config_path: str | Path) -> CampaignConfig:
    path = Path(config_path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Campaign config must be a JSON object")

    campaign_id = raw.get("campaign_id")
    if not isinstance(campaign_id, str) or not campaign_id.strip():
        raise ValueError("campaign_id must be a non-empty string")

    base_dir = path.parent
    repo_root = _resolve_path(base_dir, raw.get("repo_root"), base_dir)
    runtime_root = _resolve_path(
        base_dir,
        raw.get("runtime_root"),
        repo_root / ".cpp-perf" / "campaigns" / campaign_id,
    )

    raw_discover = raw.get("discover", {})
    if not isinstance(raw_discover, dict):
        raise ValueError("discover must be an object")
    include_globs = tuple(raw_discover.get("include_globs") or DEFAULT_INCLUDE_GLOBS)
    exclude_globs = tuple(raw_discover.get("exclude_globs") or DEFAULT_EXCLUDE_GLOBS)
    shard_depth = int(raw_discover.get("shard_depth", 2))
    max_targets_raw = raw_discover.get("max_targets")
    max_targets = int(max_targets_raw) if max_targets_raw is not None else None

    raw_budget = raw.get("budget", {})
    if not isinstance(raw_budget, dict):
        raise ValueError("budget must be an object")
    budget = BudgetConfig(
        max_attempts_per_target=int(raw_budget.get("max_attempts_per_target", 4)),
        max_low_gain_streak=int(raw_budget.get("max_low_gain_streak", 2)),
        stale_after_seconds=int(raw_budget.get("stale_after_seconds", 1800)),
        heartbeat_interval_seconds=int(raw_budget.get("heartbeat_interval_seconds", 5)),
    )

    raw_selection = raw.get("selection", {})
    if not isinstance(raw_selection, dict):
        raise ValueError("selection must be an object")
    selection = SelectionConfig(
        keep_min_speedup=float(raw_selection.get("keep_min_speedup", 1.05)),
        low_gain_speedup=float(raw_selection.get("low_gain_speedup", 1.01)),
    )

    raw_strategies = raw.get("strategies") or [
        {"name": "vectorize", "kind": "explore"},
        {"name": "layout", "kind": "exploit"},
        {"name": "branch", "kind": "explore"},
    ]
    strategies = tuple(
        StrategyConfig(
            name=str(item["name"]),
            kind=str(item.get("kind", "explore")),
            weight=float(item.get("weight", 1.0)),
            max_passes=max(1, int(item.get("max_passes", 1))),
        )
        for item in raw_strategies
        if isinstance(item, dict) and "name" in item
    )
    if not strategies:
        raise ValueError("At least one strategy is required")

    raw_hooks = raw.get("hooks")
    if not isinstance(raw_hooks, dict):
        raise ValueError("hooks must be an object")

    return CampaignConfig(
        config_path=path,
        campaign_id=campaign_id,
        repo_root=repo_root,
        runtime_root=runtime_root,
        discover=DiscoverConfig(
            include_globs=include_globs,
            exclude_globs=exclude_globs,
            shard_depth=shard_depth,
            max_targets=max_targets,
        ),
        budget=budget,
        selection=selection,
        strategies=strategies,
        hooks=_load_hooks(raw_hooks),
    )
