# DuckDB Campaign Smoke Test

## Why DuckDB

DuckDB is a strong first large-scale test target for the `cpp-perf` campaign controller because it has:

- a large active C++ core
- a clean directory structure
- an existing benchmark runner
- a practical build flow
- enough performance-critical code to stress scheduling, frontier management, and long-running optimization

Relevant DuckDB areas:

- `src/execution/`
- `src/function/`
- `src/optimizer/`
- `src/storage/`
- `src/parallel/`
- `benchmark/micro/`

## Local Prep

Clone DuckDB:

```bash
git clone --depth 1 https://github.com/duckdb/duckdb.git /tmp/duckdb-cpp-perf
```

Build the benchmark runner:

```bash
cd /tmp/duckdb-cpp-perf
BUILD_BENCHMARK=1 BUILD_TPCH=1 make
```

List benchmarks:

```bash
build/release/benchmark/benchmark_runner --list
```

Run a single benchmark:

```bash
build/release/benchmark/benchmark_runner benchmark/micro/nulls/no_nulls_addition.benchmark
```

## Campaign Config

Use `tools/cpp_perf_campaign/duckdb_campaign.json` as the starting point.

Replace:

- `repo_root`

The DuckDB smoke-test hooks are already wired in:

- `tools/cpp_perf_campaign/hooks/duckdb_prepare_case.py`
- `tools/cpp_perf_campaign/hooks/duckdb_baseline.py`
- `tools/cpp_perf_campaign/hooks/duckdb_optimize.py`
- `tools/cpp_perf_campaign/hooks/duckdb_benchmark.py`

By default, `duckdb_optimize.py` invokes `claude -p` and asks Claude Code to use the local `cpp-perf` skill
methodology for one bounded optimization attempt.
It now also post-processes optimize results with conservative terminal-state inference so the campaign can stop for explicit reasons instead of drifting forever.

Useful environment variables:

- `CPP_PERF_DUCKDB_CMAKE_BIN` — explicit `cmake` path for rebuilds; useful when `cmake` is not on `PATH`
- `CPP_PERF_CLAUDE_MODEL` — Claude model override (default: `sonnet`)
- `CPP_PERF_CLAUDE_EFFORT` — reasoning effort override (default: `medium`)
- `CPP_PERF_CLAUDE_CLEAN_MODE` — enable clean unattended mode (default: `1`)
- `CPP_PERF_CLAUDE_SETTING_SOURCES` — setting sources in clean mode (default: `project,local`)
- `CPP_PERF_CLAUDE_MCP_CONFIG` — explicit MCP config in clean mode (default: `{"mcpServers":{}}`)
- `CPP_PERF_CLAUDE_PERMISSION_MODE` — permission mode for unattended runs (default: `bypassPermissions`)
- `CPP_PERF_CLAUDE_TIMEOUT_SECONDS` — Claude optimize timeout (default: `600`)
- `CPP_PERF_PLATFORM_PROFILE` — optional profile name/path, e.g. `cortex-a78` or `/abs/path/profile.yaml`
- `CPP_PERF_PLATFORM_CONTEXT` — extra plain-text target-board context
- `CPP_PERF_AUTO_TERMINAL_MIN_PASS` — minimum strategy pass before auto terminal inference is allowed (default: `2`)
- `CPP_PERF_NO_MORE_IDEAS_STREAK` — consecutive dead-end attempts required before auto `no_more_ideas` (default: `3`)

If you already have a separate production optimizer backend, set `CPP_PERF_OPTIMIZER_BACKEND`.
The hook layer forwards the controller environment and expects that backend to emit a JSON object to `stdout`
or write it to `CPP_PERF_OPTIMIZE_STATE_PATH`.

Each generated DuckDB case now records `build_capability` in `manifest.json`.
If rebuilds are unavailable, the optimize prompt tells Claude not to propose source edits that would require recompilation.
Before reusing an existing `build/release/benchmark/benchmark_runner`, the hook now verifies that
`build/release/CMakeCache.txt` points at the current DuckDB checkout. Foreign copied build directories are discarded
and rebuilt instead of being reused silently.
When the existing release build is valid, rebuilds now target `benchmark_runner` directly instead of invoking the
default top-level build target.

## Terminal States

DuckDB optimize attempts can finish a target with:

- `hardware_limit`
- `no_more_ideas`

Preferred behavior:

- Claude sets `terminal_state` explicitly when it has a strong reason.
- If Claude does not set it, `duckdb_optimize.py` may infer it conservatively.

Current conservative inference rules:

- `hardware_limit`
  Only inferred when the current attempt makes no edit, there is at least one prior successful direction, no planned follow-up strategies remain, and the optimize summary/notes contain clear hardware-bound language such as `memory-bound`, `bandwidth-bound`, or `already vectorized`.
- `no_more_ideas`
  Only inferred when the current attempt makes no edit, no planned follow-up strategies remain, and the target has accumulated a configurable streak of recent dead ends.

If neither condition is met, the controller keeps refining.

## Suggested First Frontier

Do not begin with all of DuckDB.
Start with a narrow frontier:

1. `src/execution/`
2. `src/function/`
3. `src/storage/`

These areas are large enough to stress the controller, but focused enough to keep the campaign interpretable.

## Suggested First Run

Initialize and discover:

```bash
python3 -m tools.cpp_perf_campaign init tools/cpp_perf_campaign/duckdb_campaign.json
python3 -m tools.cpp_perf_campaign discover tools/cpp_perf_campaign/duckdb_campaign.json
python3 -m tools.cpp_perf_campaign status tools/cpp_perf_campaign/duckdb_campaign.json
```

Run a bounded smoke test:

```bash
python3 -m tools.cpp_perf_campaign run-loop \
  tools/cpp_perf_campaign/duckdb_campaign.json \
  --worker-id duckdb-worker-0 \
  --max-iterations 5
```

If that is stable, remove the iteration cap and let it run continuously.

## What to Watch

For the first DuckDB campaign, watch these signals:

- cases are generated for real DuckDB files without manual babysitting
- state survives interruption
- watchdog requeues stuck work
- targets continue across multiple strategies instead of stopping after one small win
- results remain stable enough to rank improvements meaningfully

## Exit Criteria for the Smoke Test

The smoke test is successful if:

1. the controller survives an interruption and resumes
2. at least one DuckDB target runs through multiple strategies
3. the campaign produces persistent experiment history under `.cpp-perf/campaigns/duckdb-smoke/`
4. no target gets stranded forever in `running`
