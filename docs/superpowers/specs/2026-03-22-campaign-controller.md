# Continuous Campaign Controller for cpp-perf

## Goal

Turn `cpp-perf` from a one-shot optimization skill into a campaign-driven system that can:

- keep optimizing a large repository over long periods
- survive agent interruption or machine restarts
- maintain a ranked frontier of targets
- continue exploring alternative optimization strategies instead of stopping too early

## What This Adds

The controller under `tools/cpp_perf_campaign/` provides:

- `init`: initialize runtime state
- `discover`: scan a large C++ repository into a target frontier
- `run-once`: run one optimization experiment
- `run-loop`: keep running until stopped
- `watchdog`: requeue stale running experiments
- `status`: summarize current campaign state

Runtime state lives under:

```text
.cpp-perf/campaigns/<campaign-id>/
  state.db
  heartbeat.json
  cases/
  targets/
    000123.latest.json
    000123.history.jsonl
```

## Design Notes

### Persistent state

State is stored in SQLite so the controller can resume after interruption.
Per-target memory is also materialized as JSON so hooks and agents can carry forward prior findings.

Tables:

- `targets`
- `experiments`
- `meta`

Per-target files:

- `targets/<id>.latest.json` — latest synthesized memory snapshot for one target
- `targets/<id>.history.jsonl` — append-only experiment journal for one target
- `cases/.../experiment.summary.json` — normalized summary for one specific run
- `cases/.../target_memory.json` — target memory snapshot injected into the current attempt

### Frontier and sharding

Targets are discovered from C++ source/header globs and grouped into shards by directory depth.
An optional frontier JSONL file can boost or seed target priority from production hotspots.

Example JSONL line:

```json
{"path": "src/executor/pipeline.cpp", "priority": 9.5, "reason": "prod hotspot"}
```

### Anti-early-stop behavior

The controller keeps multiple strategies per target and does not stop after one success.
It only marks a target `completed` when a hook reports an explicit terminal state.

Strategy ordering:

- before any keep: prioritize `explore`
- after a keep: prioritize `exploit`
- strategies with `max_passes > 1` may re-enter the queue for bounded refine passes after a `keep` or `low_gain`

Completion is driven by hook-provided terminal states:

- `hardware_limit` — the target appears to be at a practical hardware ceiling
- `no_more_ideas` — the optimizer has exhausted credible next moves

If no terminal state is reported, the controller requeues the target and keeps refining.
When the configured strategy ladder runs out, it falls back to another refine pass on the best known promising direction instead of stopping early.

Each attempt also receives the target memory snapshot, which contains:

- recent experiments and outcomes
- per-strategy best speedup and last result
- per-strategy remaining passes and current refine eligibility
- successful directions worth building on
- dead ends to avoid repeating without new evidence

### Watchdog

Each running experiment updates `heartbeat.json` and the SQLite state.
If a worker dies or gets interrupted, `watchdog` requeues stale targets instead of leaving them stranded in `running`.

## Hook Contract

The controller is intentionally generic. It does not assume one specific case generator or optimizer implementation.
Instead, it drives four hooks from the campaign config:

- `prepare_case`
- `baseline`
- `optimize`
- `benchmark`

The controller injects environment variables such as:

- `CPP_PERF_HOOK_NAME`
- `CPP_PERF_RESULT_PATH`
- `CPP_PERF_TARGET_PATH`
- `CPP_PERF_STRATEGY`
- `CPP_PERF_CASE_DIR`
- `CPP_PERF_EXPERIMENT_ID`
- `CPP_PERF_TARGET_MEMORY_PATH`
- `CPP_PERF_TARGET_HISTORY_PATH`
- `CPP_PERF_STRATEGY_PASS`

Hooks may write JSON to `CPP_PERF_RESULT_PATH`.

Benchmark/baseline payloads should include either:

```json
{"median_ns": 1234.0, "stable": true, "correctness": true}
```

or the existing benchmark-template shape:

```json
{"stats": {"median": 1234.0, "p99": 1300.0, "stable": true}}
```

To finish a target intentionally, a hook may return:

```json
{"terminal_state": "hardware_limit"}
```

or:

```json
{"terminal_state": "no_more_ideas"}
```

## Example

See `tools/cpp_perf_campaign/example_campaign.json`.

Each strategy may also declare `max_passes`:

```json
{"name": "layout", "kind": "exploit", "max_passes": 2}
```

This lets the controller run a bounded follow-up pass for a promising strategy instead of treating every strategy as one-shot only.
If the target is still not done after those planned passes, the scheduler continues refining the best known direction until a terminal state is reached.

Typical flow:

```bash
python3 -m tools.cpp_perf_campaign init tools/cpp_perf_campaign/example_campaign.json
python3 -m tools.cpp_perf_campaign discover tools/cpp_perf_campaign/example_campaign.json --frontier-jsonl hotspots.jsonl
python3 -m tools.cpp_perf_campaign run-loop tools/cpp_perf_campaign/example_campaign.json --worker-id worker-0
```

To stop gracefully:

```bash
touch .cpp-perf/campaigns/demo/STOP
```
