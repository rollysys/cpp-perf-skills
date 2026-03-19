# C++ Performance Optimization Skill — Design Spec

## Overview

A Claude Code superpowers skill (`cpp-perf`) that automatically analyzes and optimizes C++ code for target platforms (primarily ARM, also X86). It operates as a single-skill pipeline: static analysis → performance report → benchmark generation → cross-compilation → disassembly analysis → remote execution → data-driven optimization.

**Audience**: Open-source community (Claude Code users).

## Skill File Structure

The skill is a Claude Code superpowers skill, consisting of:

```
skills/cpp-perf/
  SKILL.md              # Skill metadata (name, description, trigger rules)
  cpp-perf.md           # Main skill instructions — pipeline stages, tool usage, output formats
  profiles/             # Platform performance profiles (YAML)
    cortex-a78.yaml
    cortex-a55.yaml
    neoverse-n1.yaml
    x86-skylake.yaml
  knowledge/            # Optimization knowledge base
    patterns/           # Structured optimization patterns extracted from references
      vectorization/
      memory/
      branching/
      compute/
      system/
    libraries.yaml      # High-performance library alternatives registry
  profiler/             # C++ platform profiler — generates profile YAML from measurements
    main.cpp
    measure_compute.cpp
    measure_cache.cpp
    measure_memory.cpp
    measure_branch.cpp
    measure_os.cpp        # OS overhead: syscall, thread, sync, scheduling
    measure_alloc.cpp     # Memory allocation: malloc/free, mmap, page faults
    measure_io.cpp        # File I/O: open/close, read/write, fsync
    measure_ipc.cpp       # IPC: pipe, eventfd, signal delivery
    output.cpp
    CMakeLists.txt
  templates/            # Benchmark code templates
    benchmark.cpp.tmpl  # Benchmark skeleton with timing/reporting harness
```

The main instruction file (`cpp-perf.md`) tells Claude how to execute each pipeline stage: what tools to call (Read, Grep, Bash for compilation/SSH), how to format outputs, and how to transition between stages.

## Knowledge Sources

The skill's optimization ability comes from three sources, not just the LLM's built-in knowledge:

### 1. Optimization Knowledge Base (`knowledge/`)

Structured optimization patterns extracted from professional references. Each pattern is a standalone markdown file:

```
knowledge/
  patterns/
    vectorization/
      auto-vectorization-blockers.md    # from perf-book Ch.9
      manual-neon-idioms.md             # from ComputeLibrary examples
      sve-scalable-patterns.md          # from optimized-routines
    memory/
      aos-to-soa.md                     # from perf-ninja labs
      loop-tiling.md                    # from perf-book Ch.8
      prefetch-strategies.md            # from Cortex-A78 optimization guide
      false-sharing.md                  # from perf-ninja labs
    branching/
      branch-to-cmov.md                # from perf-ninja labs
      lookup-table-replace.md          # from perf-ninja labs
    compute/
      dependency-chain-breaking.md     # from perf-ninja labs
      fma-utilization.md               # from optimized-routines
      strength-reduction.md            # from Cpp-High-Performance
    system/
      huge-pages.md                    # from perf-ninja labs
      alignment.md                     # from perf-book Ch.8
```

Each pattern file follows a fixed structure:

```markdown
---
name: Loop Tiling for Cache Locality
source: perf-book Ch.8, perf-ninja memory_bound/loop_tiling_1
layers: [microarchitecture, system]
platforms: [arm, x86]
keywords: [nested loop, 2D array, matrix, stride, cache miss, working set]
---

## Problem
<what code pattern triggers this optimization>

## Detection
<how to identify this in source code or disassembly>

## Transformation
<before/after code, with explanation>

## Expected Impact
<quantified estimate based on cache parameters>

## Caveats
<when NOT to apply, edge cases>
```

Valid `layers` values (matching Stage 2 analysis layers):
- `algorithm` — algorithm/data structure level
- `language` — C++ language feature level
- `microarchitecture` — SIMD, branch prediction, instruction-level
- `system` — memory alignment, OS, cache layout

During Stage 2 (Static Analysis), the skill reads relevant pattern files based on the detected code characteristics, supplementing LLM knowledge with documented, source-backed optimization techniques.

**Sources for extraction**:
- `reference/perf-book/` — theory and methodology (Ch.8-13)
- `reference/perf-ninja/` — hands-on patterns with before/after code
- `reference/Cpp-High-Performance/` — C++ language-level optimizations
- `reference/ComputeLibrary/` — production ARM NEON/SVE patterns
- `reference/optimized-routines/` — ARM-optimized library implementations
- ARM Cortex-A78 Software Optimization Guide — microarchitecture-specific guidance

### 2. High-Performance Library Registry (`knowledge/libraries.yaml`)

A mapping of standard library calls to high-performance alternatives:

```yaml
containers:
  std::unordered_map:
    alternatives:
      - name: absl::flat_hash_map
        header: "absl/container/flat_hash_map.h"
        lib: abseil-cpp
        advantage: "Open addressing, less pointer chasing, ~2x faster lookup"
        integration: drop-in  # drop-in | minor-api-change | major-refactor
        platforms: [arm, x86]
      - name: robin_hood::unordered_map
        header: "robin_hood.h"
        lib: robin-hood-hashing
        advantage: "Robin hood hashing, very fast for small-medium maps"
        integration: drop-in
        platforms: [arm, x86]
  std::map:
    alternatives:
      - name: absl::btree_map
        header: "absl/container/btree_map.h"
        lib: abseil-cpp
        advantage: "B-tree layout, cache-friendly, better for iteration"
        integration: drop-in
        platforms: [arm, x86]
  std::vector:
    note: "Usually optimal; consider folly::small_vector for small-size-optimized cases"

algorithms:
  std::sort:
    alternatives:
      - name: pdqsort
        header: "pdqsort.h"
        lib: header-only (public domain)
        advantage: "Pattern-defeating quicksort, faster on partially sorted data"
        integration: drop-in
        platforms: [arm, x86]
  std::find:
    note: "For large collections, suggest container change rather than algorithm change"

strings:
  std::string:
    alternatives:
      - name: folly::fbstring
        header: "folly/FBString.h"
        lib: folly
        advantage: "SSO up to 23 bytes, COW for large strings"
        integration: drop-in
        platforms: [arm, x86]
      - name: absl::string_view
        header: "absl/strings/string_view.h"
        lib: abseil-cpp
        advantage: "Zero-copy for read-only access"
        integration: minor-api-change
        min_std: c++11  # unnecessary in c++17+ where std::string_view exists
        platforms: [arm, x86]

memory:
  malloc/new:
    alternatives:
      - name: jemalloc
        lib: jemalloc
        advantage: "Less fragmentation, better multithreaded scaling"
        integration: drop-in  # LD_PRELOAD or link-time, no code change
        platforms: [arm, x86]
      - name: mimalloc
        lib: mimalloc
        advantage: "Compact, fast, good for ARM"
        integration: drop-in
        platforms: [arm, x86]

math:
  standard math.h:
    alternatives:
      - name: Eigen
        header: "Eigen/Dense"
        lib: eigen3
        advantage: "Vectorized linear algebra, expression templates"
        integration: major-refactor  # requires rewriting computation code
        platforms: [arm, x86]
      - name: SLEEF
        header: "sleef.h"
        lib: sleef
        advantage: "Vectorized math functions (sin/cos/exp), NEON-optimized"
        integration: minor-api-change
        platforms: [arm, x86]
```

During Stage 2, when the skill detects standard library calls, it consults this registry and includes relevant alternatives in the performance report with their trade-offs.

### 3. Platform Profiler (`profiler/`)

C++ programs that measure actual hardware characteristics on a target platform, generating the profile YAML automatically.

```
profiler/
  main.cpp              # Entry point, runs all measurement suites
  measure_compute.cpp   # Instruction latency & throughput (like MegPeak)
  measure_cache.cpp     # Cache size, line size, associativity, latency per level
  measure_memory.cpp    # Bandwidth, TLB, page sizes
  measure_branch.cpp    # Branch misprediction penalty
  measure_system.cpp    # Syscall overhead, context switch cost
  CMakeLists.txt        # Cross-compilable build
  output.cpp            # Generates profile YAML from measurements
```

**Usage flow**:

```
1. User connects a new target board
2. Skill cross-compiles the profiler: aarch64-linux-gnu-g++ ...
3. scp upload to target board
4. Run profiler on target → outputs YAML to stdout
5. Save as profiles/<board-name>.yaml
6. Subsequent optimizations use this measured profile
```

**Measurement techniques** (inspired by MegPeak and lmbench):

| Category | Measurement | Method |
|----------|-------------|--------|
| **Compute** | Instruction latency | Dependent instruction chains (data dependency forces serial execution) |
| | Instruction throughput | Independent instruction streams (saturate execution units) |
| **Cache/Memory** | Cache sizes & latency | Pointer-chasing with varying working set sizes, detect latency jumps |
| | Memory bandwidth | Sequential read/write of large arrays, measure bytes/cycle |
| **Branch** | Branch misprediction | Alternating predictable vs random branch patterns, measure delta |
| **Process/Thread** | Syscall overhead | Repeated `getpid()` or similar lightweight syscall |
| | Thread create/destroy | `pthread_create` + `pthread_join` round-trip |
| | Fork overhead | `fork` + `_exit` + `waitpid` round-trip |
| | CPU migration | Force thread to different core via affinity, measure penalty |
| **Synchronization** | Mutex lock/unlock | Uncontended `pthread_mutex_lock/unlock` cycle |
| | Spinlock | Uncontended custom spinlock cycle |
| | RWLock | Uncontended read-lock and write-lock separately |
| | Futex | Raw `futex(FUTEX_WAKE/WAIT)` round-trip |
| | Atomic operations | `atomic<int>` with `seq_cst`, `acquire/release`, `relaxed` |
| **Memory Mgmt** | malloc/free | Various sizes: 16B, 64B, 256B, 1KB, 4KB, 64KB, 1MB |
| | mmap/munmap | Anonymous mapping create/destroy cycle |
| | Minor page fault | Access freshly mmap'd page |
| | Major page fault | Access page after `madvise(DONTNEED)` |
| | Huge page alloc | `mmap` with `MAP_HUGETLB`, measure vs regular page |
| **File I/O** | open/close | Repeated open + close of same file |
| | read/write | Various block sizes: 512B, 4KB, 64KB, 1MB (on tmpfs to isolate from disk) |
| | fsync | `write` + `fsync` round-trip |
| **IPC** | Pipe throughput | `pipe` read/write round-trip, various message sizes |
| | eventfd | `eventfd_write` + `eventfd_read` round-trip |
| | Signal delivery | `kill(getpid(), SIGUSR1)` + handler return |
| **Scheduling** | sched_yield | `sched_yield()` round-trip |
| | Sleep precision | `nanosleep` requested vs actual, various durations |
| | Timer resolution | `clock_gettime` successive call delta |

The profiler is designed to be extensible — adding new measurements requires only adding a new `measure_*.cpp` file and registering it in `main.cpp`. The YAML output schema grows accordingly under the relevant section.

**Output schema**: The profiler outputs YAML conforming to the Performance Profile schema (see Configuration Files section). Fields are split into two categories:

| Category | Fields | Source |
|----------|--------|--------|
| Measured | `cache.*` (sizes, latencies), `instructions.*` (latency, throughput), `branch.mispredict_penalty`, `memory_system.tlb_miss_penalty`, `os_overhead.*` | Profiler measurements |
| Looked up | `pipeline.*` (issue_width, reorder_buffer, functional_units), `registers.*`, `branch.predictor_type`, `memory_system.load/store_queue_depth` | CPU model detection via `/proc/cpuinfo` or equivalent, matched against a built-in lookup table of known microarchitectures |

For unknown CPU models, looked-up fields are omitted and the user is prompted to fill them manually from documentation.

**Local execution**: If the user is running Claude Code directly on the target machine (e.g., an ARM developer laptop), the profiler can be compiled and run locally without SSH.

**Optional for initial release**: The skill works with hand-authored or pre-shipped profile YAML files. The profiler is an enhancement that automates profile generation — it is not a blocking dependency for the core optimization pipeline.

## Input Modes

The skill accepts three input forms:

| Input | Detection | Behavior |
|-------|-----------|----------|
| Code snippet | User pastes code or references a code block | Extract code, infer language features and context |
| Git diff | User mentions PR/commit/diff, or pastes diff-format content | Parse diff, locate changed functions/classes, read full source for context |
| File/function reference | User says "optimize this file/function" | Use Grep/Read to locate code, extract function and its call chain |

Regardless of input mode, the skill auto-expands context: read related headers, call chain (up to 2 levels deep or until 30% of context window is consumed, whichever comes first), and type definitions directly referenced by the target code.

## Pipeline Stages

The pipeline has 6 stages. Stages 4-6 each contain sub-steps (compile, disassemble, execute) but are grouped by their logical purpose.

### Stage 1: Input Parsing

- Identify input mode (snippet / diff / file reference)
- Extract target code
- Expand context (headers, call chain up to 2 levels, type definitions)
- Identify project build system if present (CMake, Bazel, Meson, Makefile) — needed for Stage 4

### Stage 2: Static Analysis

Four-layer scan, each informed by the target platform's performance profile:

| Layer | Checks | Examples |
|-------|--------|----------|
| Algorithm / Data Structure | Complexity, container choice, unnecessary sort/search | `vector` linear scan → `unordered_map` |
| Language | Redundant copies, missing move/emplace, string concat, virtual call overhead | `push_back(obj)` → `emplace_back(...)` |
| Microarchitecture | Vectorization opportunities, branch-heavy loops, data dependency chains, AoS→SoA | Scalar loop → NEON intrinsics |
| System | Memory alignment, false sharing, cache-unfriendly access patterns | Cross-cache-line struct, column-major 2D array traversal |

**Analysis procedure**:

1. **Identify relevant layers**: determine which of the four layers apply to the target code
2. **Consult knowledge base**: use `keywords` in pattern frontmatter to pre-filter, then read matching pattern files from `knowledge/patterns/` for the relevant layers. Context budget: up to 20% of context window for knowledge base reads
3. **Consult library registry**: scan code for standard library calls, look up `knowledge/libraries.yaml` for high-performance alternatives
4. **Score issues**: estimate performance impact using platform profile data + knowledge base patterns. Estimates are qualified with confidence levels:
   - **High confidence**: estimate based on instruction counts and known latencies
   - **Medium confidence**: estimate involves cache behavior assumptions
   - **Low confidence**: estimate depends on runtime data patterns

### Stage 3: Performance Report & User Decision

Output a graded report:

```
## Performance Analysis Report

### High Impact (estimated >20% improvement)
1. [P1] Loop not vectorized — foo.cpp:42 inner loop can be NEON-vectorized
   - Current: scalar element-by-element, estimated N cycles/iter
   - After: 4-wide NEON, estimated N/4 cycles/iter
   - Confidence: HIGH (pure instruction count)
   - Dependency: need to confirm data alignment

### Medium Impact (estimated 5-20%)
2. [P2] Unnecessary copy — bar.cpp:18 passing large object by value
   ...

### Low Impact (estimated <5%)
3. [P3] Branch prediction unfriendly — ...
```

Skill then asks: **"Which items do you want to optimize? Enter numbers, e.g. 1,2"**

**User can stop here** — taking only the report without proceeding to benchmarking.

### Stage 4: Benchmark, Compile & Baseline Measurement

#### 4a. Benchmark Generation

For each selected issue, generate a standalone benchmark program.

**Auto-construction strategy for test data**:

| Parameter Type | Strategy |
|----------------|----------|
| Primitives (int, float...) | Random values + boundary values |
| STL containers | Fill with typical sizes (small/medium/large) |
| Custom class/struct | Read definition, recursively construct members |
| Pointer/reference | Heap-allocate, construct valid references |
| Cannot infer | Mark as `/* TODO: user please fill */`, ask interactively |

**Interactive supplement**: For parts that cannot be auto-inferred, the skill asks specific questions:

> "The `DataChunk` parameter in `process(DataChunk& chunk)` — how large is it typically? How many elements?"

**Handling project dependencies**: If the target code depends on project-internal headers or libraries:
1. Identify required includes from the source
2. Copy needed header files alongside the benchmark
3. If the project has a build system, attempt to extract compile flags via `cmake --build --verbose` / build system introspection
4. For complex dependencies, ask the user to provide compile flags or a minimal set of sources

**Benchmark output format** — all benchmarks output JSON to stdout for reliable parsing:

```json
{
  "function": "foo",
  "iterations": 10000,
  "warmup": 1000,
  "timings_ns": [1250, 1248, 1253, ...],
  "stats": {
    "min": 1245,
    "median": 1250,
    "mean": 1252,
    "p99": 1380,
    "stddev": 42
  }
}
```

**Benchmark code structure**:

```cpp
#include <chrono>
#include <vector>
#include <cstdio>
// ... includes

auto setup_data() { /* construct test data */ }

void baseline(Args...) { /* original implementation */ }

int main() {
    auto data = setup_data();
    // warmup
    for (int i = 0; i < WARMUP; i++) baseline(data);
    // measure
    std::vector<long long> timings;
    for (int i = 0; i < ITERATIONS; i++) {
        auto t0 = std::chrono::high_resolution_clock::now();
        baseline(data);
        auto t1 = std::chrono::high_resolution_clock::now();
        timings.push_back(
            std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count());
    }
    // output JSON to stdout
    print_json(timings);
}
```

Design decisions:
- `chrono` for portability (not platform-specific timers)
- Warmup phase to exclude cold-start effects
- JSON output for reliable machine parsing
- One benchmark per issue, no interference between benchmarks

#### 4b. Cross-Compilation

Compile on the host machine using the configured cross-compiler:

```
aarch64-linux-gnu-g++ -O2 -march=armv8.2-a benchmark_p1.cpp -o benchmark_p1
```

On compilation failure: display errors, skill attempts auto-fix, asks user if it cannot resolve.

#### 4c. Disassembly Analysis

After successful compilation, disassemble using the cross-toolchain's objdump (not the host objdump, which on macOS cannot read ELF binaries):

```
aarch64-linux-gnu-objdump -d benchmark_p1 | <extract target function>
```

The disassembler command is derived from the configured compiler (replace `g++` / `gcc` with `objdump` in the toolchain prefix). Optionally, also use compiler optimization reports (`-fopt-info-vec-missed` for GCC, `-Rpass-missed=loop-vectorize` for Clang) during compilation to understand *why* the compiler made certain decisions.

Analysis checklist:

| Check | Focus |
|-------|-------|
| Vectorization | NEON instructions present? Did compiler auto-vectorize? |
| Loop structure | Unrolling degree, branch instruction count |
| Instruction selection | Efficient instructions used? (e.g. fma vs mul+add) |
| Register pressure | Excessive spills to stack? |
| Memory access pattern | Sequential vs scattered, prefetch instructions inserted? |

This step **validates or corrects** the static analysis judgment. If static analysis said "loop not vectorized" but disassembly shows NEON instructions, the issue is retracted.

Output example:

```
## Disassembly Analysis — foo.cpp:42 baseline

Compiler did NOT auto-vectorize this loop, confirming optimization opportunity:
  .L4:
    ldr   s0, [x1, x2, lsl #2]    // scalar load
    fmul  s0, s0, s1               // scalar multiply
    str   s0, [x0, x2, lsl #2]    // scalar store
    add   x2, x2, #1
    cmp   x2, x3
    b.lt  .L4

Expected after optimization: ld1/fmul(vector)/st1 sequence
```

#### 4d. Remote Execution

1. SSH connect to target board
2. `scp` upload compiled binary
3. Execute on target, capture stdout (JSON)
4. Parse JSON results locally

Error handling:

| Phase | Failure | Handling |
|-------|---------|----------|
| SSH connection | Cannot connect | Prompt user to check config, provide ssh command for manual test |
| Runtime | Crash/timeout | Show error, check if test data is the problem |
| Data | Anomalous results | If stddev too high, auto-increase iterations and re-run |

#### 4e. Baseline Data Analysis

Compare benchmark results against static analysis estimates. If results significantly deviate, explain possible reasons (cache effects, compiler already optimized, etc.) rather than forcing the original estimate.

### Stage 5: Optimize, Verify & Compare

For each confirmed optimization opportunity:

#### 5a. Generate Optimized Code

1. Generate optimized version of the code
2. Explain what changed and why

#### 5b. Correctness Verification

Before measuring performance, verify the optimized code produces identical results to the baseline:

1. Generate a correctness test that runs both baseline and optimized implementations with the same input
2. Compare outputs — if they differ, report the mismatch and fix the optimization before proceeding
3. For floating-point code, use an epsilon-based comparison with a configurable tolerance

#### 5c. Compile, Disassemble & Execute Optimized Version

Same sub-steps as Stage 4 (cross-compile → disassemble → remote execute), but with the optimized implementation. Disassembly confirms the expected instructions were actually generated.

#### 5d. Comparison Report

```
## Optimization Result

[P1] Loop NEON vectorization — foo.cpp:42
  Correctness: PASSED (all outputs match baseline)
  baseline:  median 1250ns
  optimized: median 318ns
  speedup: 3.93x

  Changes:
  - Scalar loop → vld1q_f32/vmulq_f32/vst1q_f32
  - Added 16-byte alignment declaration
  - Scalar tail handling for remainder elements
```

### Stage 6: Iteration (Optional)

If the optimization result is unsatisfactory, the skill proposes alternative strategies and loops back to Stage 5a. The user can also request "try a different approach."

## User Control Points

- After Stage 3: stop with report only
- During Stage 4a: interactive data supplement
- After Stage 5d: accept optimization or request alternatives
- Any stage: user can interrupt

## Configuration Files

### Platform Connection Config: `cpp-perf-platform.yaml`

```yaml
platforms:
  my-arm-board:
    # Host cross-compilation
    compiler: aarch64-linux-gnu-g++
    compiler_flags: "-O2 -march=armv8.2-a"
    sysroot: /opt/arm-sysroot  # optional

    # Target board
    host: 192.168.1.100
    port: 22                    # SSH port, default 22
    user: dev
    key: ~/.ssh/id_rsa          # SSH key path, optional (uses ssh-agent by default)
    proxy: jump-host.example    # SSH ProxyJump, optional
    arch: aarch64
    work_dir: /tmp/cpp-perf
    profile: cortex-a78         # references a profile file
```

First-time setup: the skill interactively guides the user to create this config. SSH confirmation is per-session — the skill shows the first SSH command for user approval, then auto-executes subsequent commands in the same session. The user's SSH key must be pre-configured (no interactive password prompts).

### Performance Profiles: `profiles/*.yaml`

```yaml
name: Cortex-A78
arch: aarch64
vendor: ARM

pipeline:
  issue_width: 4
  dispatch_width: 2
  reorder_buffer: 160
  functional_units:
    alu: 3
    fp: 2
    load: 2
    store: 1
    branch: 1

registers:
  gpr: 31          # general purpose registers
  neon: 32         # NEON/FP registers (128-bit)

cache:
  l1d: { size_kb: 64, line_bytes: 64, associativity: 4, latency: 4 }
  l1i: { size_kb: 64, line_bytes: 64, associativity: 4 }
  l2:  { size_kb: 256, line_bytes: 64, associativity: 8, latency: 9 }
  l3:  { size_kb: 4096, line_bytes: 64, associativity: 16, latency: 30 }

instructions:
  # lat = latency (cycles), tp = throughput (cycles per instruction)
  integer:
    add: { lat: 1, tp: 0.25 }
    mul: { lat: 3, tp: 1 }
    div: { lat: 12, tp: 12 }
  fp:
    fadd: { lat: 2, tp: 0.5 }
    fmul: { lat: 3, tp: 0.5 }
    fdiv: { lat: 7, tp: 7 }
  neon:
    vld1: { lat: 4, tp: 0.5 }
    vst1: { lat: 1, tp: 0.5 }
    vmul_f32: { lat: 4, tp: 0.5 }
    fmla_f32: { lat: 4, tp: 0.5 }
  memory:
    load: { lat: 4, tp: 0.5 }
    store: { lat: 1, tp: 0.5 }
    prefetch: { lat: 0, tp: 0.25 }

branch:
  mispredict_penalty: 11
  predictor_type: TAGE  # informational

memory_system:
  load_queue_depth: 68
  store_queue_depth: 44
  tlb_miss_penalty: 30
  page_sizes_kb: [4, 64, 2048]

os_overhead:
  # Process/Thread
  syscall: 500              # getpid() round-trip
  thread_create: 15000      # pthread_create + pthread_join
  fork: 50000               # fork + _exit + waitpid
  cpu_migration: 5000       # cross-core thread migration

  # Synchronization
  mutex_lock_unlock: 25     # uncontended pthread_mutex
  spinlock: 12              # uncontended custom spinlock
  rwlock_read: 20           # uncontended read-lock
  rwlock_write: 25          # uncontended write-lock
  futex: 100                # futex wake/wait round-trip
  atomic_seq_cst: 30        # atomic<int> seq_cst
  atomic_acq_rel: 15        # atomic<int> acquire/release
  atomic_relaxed: 1         # atomic<int> relaxed

  # Memory Management (cycles)
  malloc_16b: 50
  malloc_256b: 55
  malloc_4kb: 80
  malloc_1mb: 2000
  mmap_anon: 3000           # anonymous mmap/munmap
  minor_page_fault: 800
  major_page_fault: 50000
  huge_page_alloc: 1200

  # File I/O (cycles, on tmpfs)
  file_open_close: 3000
  read_4kb: 1500
  write_4kb: 1800
  fsync: 50000              # highly variable, disk-dependent

  # IPC
  pipe_roundtrip: 5000      # 64-byte message
  eventfd_roundtrip: 3000
  signal_delivery: 4000

  # Scheduling
  sched_yield: 2000
  timer_resolution_ns: 50   # clock_gettime precision (in ns, not cycles)
  context_switch: 3000
```

Profiles are stored in the skill's `profiles/` directory. Users extend by adding new YAML files. The skill ships with profiles for common platforms (Cortex-A78, Cortex-A55, Neoverse-N1, x86 Skylake).

Profile units are consistent: all sizes in KB (via `_kb` suffix), all latencies in cycles (bare integers), all throughputs in cycles-per-instruction.

## Skill Metadata

```yaml
name: cpp-perf
description: >
  Analyze and optimize C++ code performance for target platforms (ARM/X86).
  Trigger when: user asks to optimize C++ performance, profile code, analyze hotspots,
  vectorize, improve cache behavior, or benchmark C++ code.
  Do NOT trigger for: general C++ questions, code review without performance focus,
  non-C++ languages.
```

The skill pipeline is structured but allows user-driven flow control: users can stop after the report, skip to specific stages, or iterate on optimization strategies.
