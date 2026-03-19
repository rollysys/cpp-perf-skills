# cpp-perf Instrumentation Profiling — Design Spec

## Overview

An enhancement to the cpp-perf skill adding a **Stage 2.5: Instrumentation Profiling** — an optional stage between static analysis and benchmarking. When the skill is uncertain about where time is spent, it instruments the user's code with lightweight timing probes, runs it on the target, and uses measured data to pinpoint hotspots before optimizing.

## Design Constraints

- **Zero side effects during measurement**: no logging, no file I/O, no stdout between first and last probe event. Report generation after measurement completes may use stdout.
- **Minimal overhead**: hardware counter reads (~5-25ns per probe), no syscalls in hot path (except clock_gettime via vDSO on fallback)
- **Thread-safe without locks**: TLS buffers, no atomics on hot path
- **No dynamic allocation in hot path**: pre-allocated fixed-size buffers
- **Iterative**: skill can refine instrumentation granularity across multiple runs

## Probe Infrastructure: `cpp_perf_probe.h`

A single self-contained header file, included in the instrumented code. Zero external dependencies beyond `<cstdint>`, `<ctime>`, `<vector>`, `<mutex>`, `<cstdio>`, `<algorithm>`.

All code lives in `namespace profiler { }`.

### Timing Source

All timestamps are stored in **nanoseconds** regardless of platform. This normalizes the different counter resolutions across ARM and x86.

```cpp
namespace profiler {

inline uint64_t probe_timestamp_ns() {
#if defined(__aarch64__)
    // cntvct_el0: ARMv8 generic timer, typically 24MHz (not CPU cycles)
    // Convert to nanoseconds using counter frequency
    uint64_t val, freq;
    asm volatile("mrs %0, cntvct_el0" : "=r"(val));
    asm volatile("mrs %0, cntfrq_el0" : "=r"(freq));
    // ns = val * 1e9 / freq. Use 128-bit multiply to avoid overflow.
    return (uint64_t)((__uint128_t)val * 1000000000ULL / freq);
#elif defined(__x86_64__)
    // rdtsc: reads TSC at reference frequency
    // Convert to ns using calibrated frequency (set at init)
    uint32_t lo, hi;
    asm volatile("rdtsc" : "=a"(lo), "=d"(hi));
    uint64_t tsc = ((uint64_t)hi << 32) | lo;
    return (uint64_t)((__uint128_t)tsc * 1000000000ULL / tsc_freq());
#else
    // Portable fallback: clock_gettime via vDSO (~20-25ns on Linux)
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
#endif
}

// x86 TSC frequency calibration (called once at startup)
#if defined(__x86_64__)
inline uint64_t tsc_freq() {
    static uint64_t freq = []() -> uint64_t {
        struct timespec t0, t1;
        clock_gettime(CLOCK_MONOTONIC_RAW, &t0);
        uint32_t lo0, hi0, lo1, hi1;
        asm volatile("rdtsc" : "=a"(lo0), "=d"(hi0));
        // Spin ~10ms
        do { clock_gettime(CLOCK_MONOTONIC_RAW, &t1); }
        while ((t1.tv_sec - t0.tv_sec) * 1000000000LL + (t1.tv_nsec - t0.tv_nsec) < 10000000);
        asm volatile("rdtsc" : "=a"(lo1), "=d"(hi1));
        uint64_t dt_ns = (t1.tv_sec - t0.tv_sec) * 1000000000ULL + (t1.tv_nsec - t0.tv_nsec);
        uint64_t dt_tsc = (((uint64_t)hi1 << 32) | lo1) - (((uint64_t)hi0 << 32) | lo0);
        return (uint64_t)((double)dt_tsc / dt_ns * 1e9);
    }();
    return freq;
}
#endif

} // namespace profiler
```

Platform resolution:
- x86_64: `rdtsc` at ~3GHz → ~0.3ns resolution, converted to nanoseconds via calibrated frequency
- aarch64: `cntvct_el0` at 24MHz → ~42ns resolution. Sufficient for function-level (L1) and region-level (L2) measurement. L3 line-level probes may need aggregation across iterations for accuracy
- Fallback: `clock_gettime(CLOCK_MONOTONIC_RAW)` — ~20-25ns via vDSO, NTP-independent

### Data Structures

```cpp
namespace profiler {

struct ProbeEvent {
    uint32_t probe_id;     // Probe identifier
    uint32_t flags;        // 0 = BEGIN, 1 = END
    uint64_t timestamp_ns; // Nanoseconds
};

// Buffer size: 128K events = 2MB per thread
// Sufficient for 64K begin/end pairs (loops up to 64K iterations)
static constexpr size_t PROBE_BUF_SIZE = 131072;

struct alignas(64) ProbeBuffer {
    ProbeEvent events[PROBE_BUF_SIZE];
    uint32_t write_pos = 0;
    uint32_t thread_id = 0;
    uint32_t overflow_count = 0;  // Incremented on buffer wrap
};

static thread_local ProbeBuffer tls_probe_buf;

} // namespace profiler
```

Design notes:
- `alignas(64)`: cache-line aligned, prevents false sharing
- Buffer size 131072 (128K events, 2MB): supports loops up to 64K iterations at L2/L3 granularity
- `overflow_count`: tracks how many times the buffer wrapped, used by report generator to warn about data loss
- `ProbeEvent` is 16 bytes, cache-line friendly (4 events per cache line)

**High-iteration loop strategy**: For loops with >64K iterations, the skill instruments with **aggregated probes** — a single BEGIN before the loop and END after, rather than per-iteration probes. The total loop time is sufficient to identify it as a hotspot. Per-iteration detail is gathered in a separate L3 run with a reduced iteration count or sampling (instrument every Nth iteration using `if (i % SAMPLE_RATE == 0)`).

### Probe API

```cpp
namespace profiler {

// Core write operation
inline void probe_mark(uint32_t id, uint32_t flag) {
    auto& buf = tls_probe_buf;
    if (buf.write_pos >= PROBE_BUF_SIZE) {
        buf.overflow_count++;
        buf.write_pos = 0;  // Wrap
    }
    buf.events[buf.write_pos] = {id, flag, probe_timestamp_ns()};
    buf.write_pos++;
}

// Manual probes for arbitrary code regions
#define PROBE_BEGIN(id) profiler::probe_mark(id, 0)
#define PROBE_END(id)   profiler::probe_mark(id, 1)

// RAII scope probe — auto-pairs begin/end
struct ScopeProbe {
    uint32_t id;
    ScopeProbe(uint32_t id) : id(id) { probe_mark(id, 0); }
    ~ScopeProbe() { probe_mark(id, 1); }
};
#define PROBE_SCOPE(id) profiler::ScopeProbe _probe_##id(id)

} // namespace profiler
```

### Multi-Thread Buffer Collection

**Constraint**: `probe_report()` must be called after all worker threads have been joined. This avoids dangling TLS pointer issues.

```cpp
namespace profiler {

static std::vector<ProbeBuffer*> all_buffers;
static std::mutex buf_mutex;

struct ProbeBufferRegistrar {
    ProbeBufferRegistrar() {
        std::lock_guard<std::mutex> lk(buf_mutex);
        tls_probe_buf.thread_id = (uint32_t)all_buffers.size();
        all_buffers.push_back(&tls_probe_buf);
    }
};
static thread_local ProbeBufferRegistrar _registrar;

} // namespace profiler
```

Registration happens once per thread creation — no impact on hot path. All threads must be joined before calling `probe_report()` to ensure TLS buffers are still valid.

### Report Generation Algorithm

`probe_report()` processes all registered buffers and outputs JSON to stdout.

**Algorithm (pseudocode):**

```
function probe_report():
    // Step 1: Merge all thread buffers into one sorted event list
    merged = []
    for each buffer in all_buffers:
        if buffer.overflow_count > 0:
            emit warning to stderr
        count = min(buffer.write_pos, PROBE_BUF_SIZE)
        merged.append(buffer.events[0..count])
    sort merged by timestamp_ns

    // Step 2: Build call tree using a stack-based approach
    // Process events per-thread (events from different threads are independent)
    for each thread_id:
        thread_events = filter merged by thread_id
        stack = []  // stack of (probe_id, begin_timestamp)

        for event in thread_events:
            if event is BEGIN:
                stack.push( (event.probe_id, event.timestamp_ns) )

            elif event is END:
                if stack is empty or stack.top().probe_id != event.probe_id:
                    // Orphaned END — skip (overflow or error)
                    continue
                (id, begin_ts) = stack.pop()
                duration = event.timestamp_ns - begin_ts
                parent_id = stack.top().probe_id if stack not empty else ROOT

                // Accumulate into per-probe stats
                stats[id].calls += 1
                stats[id].total_ns += duration
                stats[id].parent = parent_id

    // Step 3: Compute self_ns
    for each probe_id in stats:
        children_total = sum(stats[child].total_ns for child where child.parent == probe_id)
        stats[probe_id].self_ns = stats[probe_id].total_ns - children_total

    // Step 4: Compute pct_of_parent
    for each probe_id in stats:
        parent = stats[probe_id].parent
        if parent == ROOT:
            stats[probe_id].pct = 100.0
        else:
            stats[probe_id].pct = stats[probe_id].total_ns / stats[parent].total_ns * 100

    // Step 5: Build nested JSON tree and output
    output JSON with tree structure
```

**Edge case handling:**
- **Orphaned BEGIN (no matching END)**: can occur from buffer overflow mid-pair. Detected when an END arrives for a different probe_id than stack top. The orphaned BEGIN is discarded and a warning emitted.
- **Orphaned END (no matching BEGIN)**: skipped silently (lost BEGIN from buffer wrap).
- **Recursive functions**: same probe_id can nest within itself. The stack handles this correctly — each BEGIN pushes a new frame regardless of ID.
- **Buffer overflow**: detected via `overflow_count > 0`. Report emits warning: `"warning: thread N buffer overflowed M times, results may be incomplete"`.
- **Multi-thread**: events are processed per-thread independently. Cross-thread call trees are not attempted.

### Report Output Schema

```json
{
  "timestamp_unit": "nanoseconds",
  "warnings": ["thread 0 buffer overflowed 3 times, results may be incomplete"],
  "probes": {
    "1": { "name": "solution()", "file": "solution.cpp", "line": 23 },
    "2": { "name": "outer_loop", "file": "solution.cpp", "line": 28 },
    "3": { "name": "inner_search", "file": "solution.cpp", "line": 31 }
  },
  "results": [
    {
      "probe_id": 1,
      "calls": 1,
      "total_ns": 450000000,
      "self_ns": 5000000,
      "pct_of_parent": 100.0,
      "children": [
        {
          "probe_id": 2,
          "calls": 10000,
          "total_ns": 445000000,
          "self_ns": 20000000,
          "pct_of_parent": 98.9,
          "children": [
            {
              "probe_id": 3,
              "calls": 10000,
              "total_ns": 425000000,
              "self_ns": 425000000,
              "pct_of_parent": 95.5
            }
          ]
        }
      ]
    }
  ]
}
```

Field naming: `total_ns` / `self_ns` (not `cycles`) — always nanoseconds.

## Skill Integration: Stage 2.5

Stage 2.5 is inserted between Stage 2 (Static Analysis) and Stage 3 (Performance Report) in `cpp-perf.md`.

### When to trigger

Stage 2.5 is **optional**. The skill invokes it when:
- Static analysis identifies multiple possible hotspots but cannot determine which dominates
- The target code is complex enough that LLM-based estimation has LOW confidence
- The user explicitly requests instrumentation ("profile this", "measure where time is spent")

The skill asks the user: "Static analysis found N potential issues but I'm not confident about their relative impact. Want me to instrument and measure? (Requires target board)"

### Iterative instrumentation flow

```
Level 1 (L1): Function-level
  → Skill identifies all function definitions
  → Inserts PROBE_SCOPE(N) at each function entry
  → Cross-compile → run on target → collect JSON
  → Parse: identify functions consuming >10% of total time

Level 2 (L2): Region-level (hot functions only)
  → For each hot function, identify: loops, branches, call sites
  → Insert PROBE_BEGIN/END around each region (around loops, not per-iteration)
  → Cross-compile → run → collect
  → Parse: identify regions consuming >20% of parent

Level 3 (L3): Line-level (hot regions only)
  → For hot regions, insert probes every 3-5 statements
  → For high-iteration loops: use sampling (probe every Nth iteration)
  → Cross-compile → run → collect
  → Parse: narrow down to specific lines
```

The skill decides when to stop iterating:
- A single probe dominates (>70% of parent) → hotspot found
- User says "enough"
- L3 reached → maximum granularity

### Auto-instrumentation rules

The skill generates instrumented code by:

1. **Parsing code structure**: identify functions, loops (for/while/do-while), if/else branches, significant call sites
2. **Assigning probe IDs**: `level * 1000 + sequence` to avoid collisions across refinement levels (L1: 1000-1999, L2: 2000-2999, L3: 3000-3999)
3. **Inserting probes**:
   - Function: `PROBE_SCOPE(N)` as first statement
   - Loop: `PROBE_BEGIN(N)` before loop, `PROBE_END(N)` after loop (measures total loop time, not per-iteration)
   - Branch: `PROBE_BEGIN(N)` at start of each branch body, `PROBE_END(N)` at end
   - Code segment: `PROBE_BEGIN(N)` / `PROBE_END(N)` around N lines
   - High-iteration loop sampling: `if (i % SAMPLE_RATE == 0) { PROBE_BEGIN(N); } ... if (i % SAMPLE_RATE == 0) { PROBE_END(N); }`
4. **Adding infrastructure**: `#include "cpp_perf_probe.h"` at top
5. **Adding report call**: in the benchmark harness (not in user code). The instrumentation benchmark template calls `profiler::probe_report()` after the measurement loop, before `print_json()`. This means the instrumented code runs inside the existing benchmark template structure — same as Stage 4, but with probes added and the report call appended.
6. **Preserving original logic**: probes are pure additions, no code modification, no reordering

### Hotspot report presentation

After collecting data, the skill presents:

```
## Instrumentation Report — solution() [L1]

  solution()            450.0ms  100.0%  ████████████████████
  getSumOfDigits()        2.1ms    0.5%  ▏

Hotspot: solution() at solution.cpp:23 — 99.5% of total
→ Drilling down to L2...

## Instrumentation Report — solution() [L2]

  solution()            450.0ms  100.0%  ████████████████████
    outer_loop (×10000) 445.0ms   98.9%  ███████████████████▉
      inner_search      425.0ms   95.5%  ███████████████████▏
    [self]                5.0ms    1.1%  ▎

Hotspot: inner_search at solution.cpp:31 — 95.5%
→ This is a pointer-chasing dependency chain in the inner loop.
→ Proceeding to Stage 3 with this data to inform optimization.
```

### Integration with existing stages

After instrumentation identifies the hotspot:
- Stage 3 report includes measured data alongside static analysis estimates
- Confidence levels are upgraded (MEDIUM→HIGH when backed by measurement)
- Stage 4 benchmark focuses on the measured hotspot
- Stage 5 optimization targets the specific code identified by instrumentation

### Integration with benchmark template

The instrumented code runs within the existing benchmark infrastructure. The skill generates an instrumented benchmark that:
1. Includes `cpp_perf_probe.h`
2. Contains the user's code with probes inserted
3. Runs the instrumented code (same as benchmark, but without per-iteration timing — the probes handle timing internally)
4. Calls `profiler::probe_report()` to output probe JSON
5. The skill parses the probe JSON (separate from benchmark JSON) to build the hotspot report

## File Structure

```
skills/cpp-perf/
  templates/
    cpp_perf_probe.h        # Self-contained probe infrastructure header
  cpp-perf.md               # Updated: Stage 2.5 added
```

The `cpp_perf_probe.h` is a template file. When the skill instruments code, it copies this header alongside the instrumented source, cross-compiles both, and runs on target.
