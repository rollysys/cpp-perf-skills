# cpp-perf Core Skill + Data — Implementation Plan (Plan 1 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a working `cpp-perf` Claude Code superpowers skill that can analyze C++ code, generate performance reports, create benchmarks, cross-compile, disassemble, execute remotely, and produce optimization recommendations — all guided by platform profile data.

**Architecture:** Single-skill pipeline in markdown instruction format. The skill file (`cpp-perf.md`) guides Claude through 6 stages via structured prompts, tool calls (Read, Grep, Bash), and output format templates. Platform profiles are YAML data. Benchmark code is generated from a C++ template.

**Tech Stack:** Markdown (skill instructions), YAML (profiles, config, library registry), C++ (benchmark template)

**Spec:** `docs/superpowers/specs/2026-03-19-cpp-perf-skill-design.md`

---

## File Structure

```
skills/cpp-perf/
  SKILL.md                      # Skill metadata — name, description, trigger rules
  cpp-perf.md                   # Main skill instructions — 6-stage pipeline
  templates/
    benchmark.cpp.tmpl          # Benchmark skeleton with JSON timing output
    correctness.cpp.tmpl        # Correctness verification template
  profiles/
    cortex-a78.yaml             # ARM Cortex-A78 performance profile
    cortex-a55.yaml             # ARM Cortex-A55 performance profile
    neoverse-n1.yaml            # ARM Neoverse-N1 performance profile
    x86-skylake.yaml            # Intel Skylake performance profile
  knowledge/
    libraries.yaml              # High-performance library alternatives registry
```

---

### Task 1: Skill Metadata (SKILL.md)

**Files:**
- Create: `skills/cpp-perf/SKILL.md`

- [ ] **Step 1: Create SKILL.md**

```markdown
---
name: cpp-perf
description: >
  Analyze and optimize C++ code performance for target platforms (ARM/X86).
  TRIGGER when: user asks to optimize C++ performance, profile C++ code, analyze
  performance hotspots, vectorize loops, improve cache behavior, benchmark C++ code,
  or mentions NEON/SIMD optimization.
  Do NOT trigger for: general C++ questions, code review without performance focus,
  non-C++ languages, build system configuration.
---
```

- [ ] **Step 2: Verify file is valid YAML frontmatter**

Run: `head -10 skills/cpp-perf/SKILL.md`
Expected: valid YAML between `---` delimiters

- [ ] **Step 3: Commit**

```bash
git add skills/cpp-perf/SKILL.md
git commit -m "feat: add cpp-perf skill metadata"
```

---

### Task 2: Benchmark Code Template

**Files:**
- Create: `skills/cpp-perf/templates/benchmark.cpp.tmpl`

- [ ] **Step 1: Create benchmark template**

The template uses placeholder markers (`{{INCLUDES}}`, `{{SETUP_DATA}}`, etc.) that the skill instructions tell Claude to replace when generating benchmarks.

```cpp
// cpp-perf auto-generated benchmark
// Target: {{FUNCTION_NAME}} — {{ISSUE_ID}}
#include <chrono>
#include <vector>
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <cmath>
{{INCLUDES}}

// ============================================================
// Prevent compiler from optimizing away the result
// ============================================================
template <typename T>
__attribute__((noinline)) void do_not_optimize(T const& value) {
    asm volatile("" : : "r,m"(value) : "memory");
}

// ============================================================
// Test data setup
// ============================================================
{{SETUP_DATA}}

// ============================================================
// Implementation under test
// ============================================================
{{IMPLEMENTATION}}

// ============================================================
// JSON output
// ============================================================
static void print_json(const char* func_name, int iterations, int warmup,
                       const std::vector<long long>& timings) {
    std::vector<long long> sorted = timings;
    std::sort(sorted.begin(), sorted.end());

    long long min_val = sorted.front();
    long long median = sorted[sorted.size() / 2];
    double mean = 0;
    for (auto t : sorted) mean += t;
    mean /= sorted.size();
    long long p99 = sorted[(size_t)(sorted.size() * 0.99)];

    double variance = 0;
    for (auto t : sorted) variance += (t - mean) * (t - mean);
    double stddev = std::sqrt(variance / sorted.size());

    printf("{\n");
    printf("  \"function\": \"%s\",\n", func_name);
    printf("  \"iterations\": %d,\n", iterations);
    printf("  \"warmup\": %d,\n", warmup);
    printf("  \"timings_ns\": [");
    for (size_t i = 0; i < timings.size(); i++) {
        if (i > 0) printf(",");
        printf("%lld", timings[i]);
    }
    printf("],\n");
    printf("  \"stats\": {\n");
    printf("    \"min\": %lld,\n", min_val);
    printf("    \"median\": %lld,\n", median);
    printf("    \"mean\": %.1f,\n", mean);
    printf("    \"p99\": %lld,\n", p99);
    printf("    \"stddev\": %.1f\n", stddev);
    printf("  }\n");
    printf("}\n");
}

// ============================================================
// Main
// ============================================================
int main() {
    const int WARMUP = {{WARMUP_COUNT}};
    const int ITERATIONS = {{ITERATION_COUNT}};

    auto data = setup_data();

    // Warmup
    for (int i = 0; i < WARMUP; i++) {
        auto result = {{FUNCTION_CALL}};
        do_not_optimize(result);
    }

    // Measure
    std::vector<long long> timings;
    timings.reserve(ITERATIONS);
    for (int i = 0; i < ITERATIONS; i++) {
        {{RESET_DATA}}
        auto t0 = std::chrono::steady_clock::now();
        auto result = {{FUNCTION_CALL}};
        do_not_optimize(result);
        auto t1 = std::chrono::steady_clock::now();
        timings.push_back(
            std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count());
    }

    print_json("{{FUNCTION_NAME}}", ITERATIONS, WARMUP, timings);
    return 0;
}
```

Key design notes for the template:
- Uses `steady_clock` (not `high_resolution_clock`) for monotonic timing
- `do_not_optimize()` prevents dead code elimination
- `{{RESET_DATA}}` placeholder for mutating functions (e.g. in-place sort)
- JSON output to stdout for machine parsing

- [ ] **Step 2: Verify template has all required placeholders**

Run: `grep -c '{{' skills/cpp-perf/templates/benchmark.cpp.tmpl`
Expected: 8+ placeholder occurrences (INCLUDES, SETUP_DATA, IMPLEMENTATION, FUNCTION_NAME, ISSUE_ID, WARMUP_COUNT, ITERATION_COUNT, FUNCTION_CALL, RESET_DATA)

- [ ] **Step 3: Commit**

```bash
git add skills/cpp-perf/templates/benchmark.cpp.tmpl
git commit -m "feat: add benchmark code template with JSON output and DoNotOptimize"
```

---

### Task 3: Correctness Verification Template

**Files:**
- Create: `skills/cpp-perf/templates/correctness.cpp.tmpl`

- [ ] **Step 1: Create correctness template**

```cpp
// cpp-perf correctness verification
// Verifies optimized output matches baseline for {{ISSUE_ID}}
#include <cstdio>
#include <cmath>
#include <cstring>
{{INCLUDES}}

{{SETUP_DATA}}

{{BASELINE_IMPLEMENTATION}}

{{OPTIMIZED_IMPLEMENTATION}}

// ============================================================
// Comparison
// ============================================================
template <typename T>
bool compare(const T& a, const T& b) {
    return a == b;
}

// Float comparison with epsilon
bool compare(float a, float b) {
    if (a == b) return true;
    float diff = std::fabs(a - b);
    float largest = std::fmax(std::fabs(a), std::fabs(b));
    return diff <= largest * {{EPSILON}};
}

bool compare(double a, double b) {
    if (a == b) return true;
    double diff = std::fabs(a - b);
    double largest = std::fmax(std::fabs(a), std::fabs(b));
    return diff <= largest * {{EPSILON}};
}

int main() {
    auto data = setup_data();

    {{CORRECTNESS_CHECK}}

    printf("{\"correctness\": \"PASSED\", \"samples\": %d}\n", {{SAMPLE_COUNT}});
    return 0;
}
```

- [ ] **Step 2: Commit**

```bash
git add skills/cpp-perf/templates/correctness.cpp.tmpl
git commit -m "feat: add correctness verification template"
```

---

### Task 4: Cortex-A78 Performance Profile

**Files:**
- Create: `skills/cpp-perf/profiles/cortex-a78.yaml`

**Reference docs:**
- `reference/Arm_Cortex-A78_Core_Software_Optimization_Guide.pdf`
- `reference/arm_cortex_a78_core_trm_101430_0102_09_en.pdf`

- [ ] **Step 1: Read ARM Cortex-A78 Software Optimization Guide to extract accurate data**

Read the PDF reference documents to verify/correct the values from the spec. Key sections:
- Pipeline characteristics (issue width, ROB size, functional units)
- Instruction latencies and throughput tables
- Cache hierarchy parameters
- Branch predictor characteristics

**Fallback**: If PDF files at `reference/Arm_Cortex-A78_Core_Software_Optimization_Guide.pdf` are not readable, use the values from the spec as-is and add a comment `# unverified — values from spec, pending PDF verification` at the top of the YAML file.

- [ ] **Step 2: Create cortex-a78.yaml with verified data**

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
  gpr: 31
  neon: 32

cache:
  l1d: { size_kb: 64, line_bytes: 64, associativity: 4, latency: 4 }
  l1i: { size_kb: 64, line_bytes: 64, associativity: 4 }
  l2:  { size_kb: 256, line_bytes: 64, associativity: 8, latency: 9 }
  l3:  { size_kb: 4096, line_bytes: 64, associativity: 16, latency: 30 }

instructions:
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
  predictor_type: TAGE

memory_system:
  load_queue_depth: 68
  store_queue_depth: 44
  tlb_miss_penalty: 30
  page_sizes_kb: [4, 64, 2048]

os_overhead:
  syscall: 500
  thread_create: 15000
  fork: 50000
  cpu_migration: 5000
  mutex_lock_unlock: 25
  spinlock: 12
  rwlock_read: 20
  rwlock_write: 25
  futex: 100
  atomic_seq_cst: 30
  atomic_acq_rel: 15
  atomic_relaxed: 1
  malloc_16b: 50
  malloc_256b: 55
  malloc_4kb: 80
  malloc_1mb: 2000
  mmap_anon: 3000
  minor_page_fault: 800
  major_page_fault: 50000
  huge_page_alloc: 1200
  file_open_close: 3000
  read_4kb: 1500
  write_4kb: 1800
  fsync: 50000
  pipe_roundtrip: 5000
  eventfd_roundtrip: 3000
  signal_delivery: 4000
  sched_yield: 2000
  timer_resolution_ns: 50
  context_switch: 3000
```

Note: `os_overhead` values are approximate — marked as such in the spec. The profiler (Plan 3) will provide measured values. Pipeline and instruction values should be verified against the ARM optimization guide PDF.

- [ ] **Step 3: Validate YAML syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('skills/cpp-perf/profiles/cortex-a78.yaml'))"`
Expected: no errors

- [ ] **Step 4: Commit**

```bash
git add skills/cpp-perf/profiles/cortex-a78.yaml
git commit -m "feat: add Cortex-A78 performance profile"
```

---

### Task 5: Cortex-A55, Neoverse-N1, Skylake Profiles

**Files:**
- Create: `skills/cpp-perf/profiles/cortex-a55.yaml`
- Create: `skills/cpp-perf/profiles/neoverse-n1.yaml`
- Create: `skills/cpp-perf/profiles/x86-skylake.yaml`

- [ ] **Step 1: Create cortex-a55.yaml**

Cortex-A55 is an in-order, 2-wide decode, efficiency core. Use LLM knowledge + ARM public documentation. Key differences from A78: narrower pipeline, smaller caches, higher latencies, typically no L3.

```yaml
name: Cortex-A55
arch: aarch64
vendor: ARM

pipeline:
  issue_width: 2
  dispatch_width: 2
  reorder_buffer: 0  # in-order core, no ROB
  functional_units:
    alu: 2
    fp: 1
    load: 1
    store: 1
    branch: 1

registers:
  gpr: 31
  neon: 32

cache:
  l1d: { size_kb: 32, line_bytes: 64, associativity: 4, latency: 3 }
  l1i: { size_kb: 32, line_bytes: 64, associativity: 2 }
  l2:  { size_kb: 256, line_bytes: 64, associativity: 8, latency: 10 }
  # No L3 on typical A55 configurations

instructions:
  integer:
    add: { lat: 1, tp: 0.5 }
    mul: { lat: 3, tp: 1 }
    div: { lat: 12, tp: 12 }
  fp:
    fadd: { lat: 4, tp: 1 }
    fmul: { lat: 4, tp: 1 }
    fdiv: { lat: 16, tp: 16 }
  neon:
    vld1: { lat: 5, tp: 1 }
    vst1: { lat: 1, tp: 1 }
    vmul_f32: { lat: 4, tp: 1 }
    fmla_f32: { lat: 6, tp: 1 }
  memory:
    load: { lat: 3, tp: 1 }
    store: { lat: 1, tp: 1 }
    prefetch: { lat: 0, tp: 1 }

branch:
  mispredict_penalty: 8
  predictor_type: bimodal

memory_system:
  load_queue_depth: 4
  store_queue_depth: 4
  tlb_miss_penalty: 20
  page_sizes_kb: [4, 64, 2048]

os_overhead:
  syscall: 600
  thread_create: 20000
  fork: 60000
  cpu_migration: 6000
  mutex_lock_unlock: 30
  spinlock: 15
  rwlock_read: 25
  rwlock_write: 30
  futex: 120
  atomic_seq_cst: 40
  atomic_acq_rel: 20
  atomic_relaxed: 1
  malloc_16b: 60
  malloc_256b: 65
  malloc_4kb: 100
  malloc_1mb: 2500
  mmap_anon: 3500
  minor_page_fault: 1000
  major_page_fault: 60000
  huge_page_alloc: 1500
  file_open_close: 3500
  read_4kb: 2000
  write_4kb: 2200
  fsync: 55000
  pipe_roundtrip: 6000
  eventfd_roundtrip: 3500
  signal_delivery: 5000
  sched_yield: 2500
  timer_resolution_ns: 80
  context_switch: 4000
```

- [ ] **Step 2: Create neoverse-n1.yaml**

Neoverse-N1 is a server-class ARM core, similar to Cortex-A76 microarchitecture. Out-of-order, 4-wide decode, larger caches for server workloads.

```yaml
name: Neoverse-N1
arch: aarch64
vendor: ARM

pipeline:
  issue_width: 4
  dispatch_width: 8
  reorder_buffer: 128
  functional_units:
    alu: 3
    fp: 2
    load: 2
    store: 1
    branch: 1

registers:
  gpr: 31
  neon: 32

cache:
  l1d: { size_kb: 64, line_bytes: 64, associativity: 4, latency: 4 }
  l1i: { size_kb: 64, line_bytes: 64, associativity: 4 }
  l2:  { size_kb: 1024, line_bytes: 64, associativity: 8, latency: 11 }
  l3:  { size_kb: 32768, line_bytes: 64, associativity: 16, latency: 40 }

instructions:
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
  mispredict_penalty: 13
  predictor_type: TAGE

memory_system:
  load_queue_depth: 56
  store_queue_depth: 36
  tlb_miss_penalty: 35
  page_sizes_kb: [4, 64, 2048]

os_overhead:
  syscall: 400
  thread_create: 12000
  fork: 40000
  cpu_migration: 4000
  mutex_lock_unlock: 20
  spinlock: 10
  rwlock_read: 18
  rwlock_write: 22
  futex: 90
  atomic_seq_cst: 28
  atomic_acq_rel: 12
  atomic_relaxed: 1
  malloc_16b: 45
  malloc_256b: 50
  malloc_4kb: 70
  malloc_1mb: 1800
  mmap_anon: 2800
  minor_page_fault: 700
  major_page_fault: 45000
  huge_page_alloc: 1100
  file_open_close: 2800
  read_4kb: 1300
  write_4kb: 1600
  fsync: 45000
  pipe_roundtrip: 4500
  eventfd_roundtrip: 2800
  signal_delivery: 3500
  sched_yield: 1800
  timer_resolution_ns: 40
  context_switch: 2500
```

- [ ] **Step 3: Create x86-skylake.yaml**

Intel Skylake is a 4-wide x86 out-of-order core. Key differences: x86 instruction mnemonics, AVX2 registers (16 YMM 256-bit), larger ROB.

```yaml
name: Skylake
arch: x86_64
vendor: Intel

pipeline:
  issue_width: 4
  dispatch_width: 6
  reorder_buffer: 224
  functional_units:
    alu: 4
    fp: 2
    load: 2
    store: 1
    branch: 2

registers:
  gpr: 16
  ymm: 16         # AVX2 256-bit registers
  zmm: 0          # No AVX-512 on client Skylake

cache:
  l1d: { size_kb: 32, line_bytes: 64, associativity: 8, latency: 4 }
  l1i: { size_kb: 32, line_bytes: 64, associativity: 8 }
  l2:  { size_kb: 256, line_bytes: 64, associativity: 4, latency: 12 }
  l3:  { size_kb: 8192, line_bytes: 64, associativity: 16, latency: 36 }

instructions:
  integer:
    add: { lat: 1, tp: 0.25 }
    imul: { lat: 3, tp: 1 }
    idiv: { lat: 26, tp: 10 }
  fp:
    addss: { lat: 4, tp: 0.5 }
    mulss: { lat: 4, tp: 0.5 }
    divss: { lat: 11, tp: 3 }
  avx2:
    vmovups: { lat: 5, tp: 0.5 }    # 256-bit load
    vmovaps_store: { lat: 1, tp: 0.5 }  # 256-bit store
    vmulps: { lat: 4, tp: 0.5 }     # 256-bit float multiply
    vfmadd: { lat: 4, tp: 0.5 }     # 256-bit FMA
  memory:
    load: { lat: 4, tp: 0.5 }
    store: { lat: 1, tp: 0.5 }
    prefetch: { lat: 0, tp: 0.25 }

branch:
  mispredict_penalty: 16
  predictor_type: TAGE

memory_system:
  load_queue_depth: 72
  store_queue_depth: 56
  tlb_miss_penalty: 40
  page_sizes_kb: [4, 2048]

os_overhead:
  syscall: 300
  thread_create: 10000
  fork: 35000
  cpu_migration: 3000
  mutex_lock_unlock: 20
  spinlock: 10
  rwlock_read: 15
  rwlock_write: 20
  futex: 80
  atomic_seq_cst: 25
  atomic_acq_rel: 12
  atomic_relaxed: 1
  malloc_16b: 40
  malloc_256b: 45
  malloc_4kb: 65
  malloc_1mb: 1500
  mmap_anon: 2500
  minor_page_fault: 600
  major_page_fault: 40000
  huge_page_alloc: 1000
  file_open_close: 2500
  read_4kb: 1200
  write_4kb: 1500
  fsync: 40000
  pipe_roundtrip: 4000
  eventfd_roundtrip: 2500
  signal_delivery: 3000
  sched_yield: 1500
  timer_resolution_ns: 30
  context_switch: 2000
```

- [ ] **Step 4: Validate all YAML files**

Run: `for f in skills/cpp-perf/profiles/*.yaml; do echo "=== $f ===" && python3 -c "import yaml; yaml.safe_load(open('$f'))" && echo "OK"; done`
Expected: all OK

- [ ] **Step 5: Commit**

```bash
git add skills/cpp-perf/profiles/cortex-a55.yaml skills/cpp-perf/profiles/neoverse-n1.yaml skills/cpp-perf/profiles/x86-skylake.yaml
git commit -m "feat: add Cortex-A55, Neoverse-N1, and Skylake profiles"
```

---

### Task 6: High-Performance Library Registry

**Files:**
- Create: `skills/cpp-perf/knowledge/libraries.yaml`

- [ ] **Step 1: Create libraries.yaml**

```yaml
# High-Performance Library Alternatives Registry
# integration values: drop-in | minor-api-change | major-refactor

containers:
  std::unordered_map:
    alternatives:
      - name: absl::flat_hash_map
        header: "absl/container/flat_hash_map.h"
        lib: abseil-cpp
        advantage: "Open addressing, less pointer chasing, ~2x faster lookup"
        integration: drop-in
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
        integration: drop-in
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
        integration: major-refactor
        platforms: [arm, x86]
      - name: SLEEF
        header: "sleef.h"
        lib: sleef
        advantage: "Vectorized math functions (sin/cos/exp), NEON-optimized"
        integration: minor-api-change
        platforms: [arm, x86]
```

- [ ] **Step 2: Validate YAML syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('skills/cpp-perf/knowledge/libraries.yaml'))"`
Expected: no errors

- [ ] **Step 3: Commit**

```bash
git add skills/cpp-perf/knowledge/libraries.yaml
git commit -m "feat: add high-performance library alternatives registry"
```

---

### Task 7: Main Skill Instructions — Stage 1 (Input Parsing)

**Files:**
- Create: `skills/cpp-perf/cpp-perf.md`

This is the core of the skill. We build it stage by stage. Each stage is a section in the markdown file that tells Claude exactly what to do.

- [ ] **Step 1: Create cpp-perf.md with header and Stage 1**

```markdown
# C++ Performance Optimization Skill

You are a C++ performance optimization expert. Follow this pipeline to analyze and optimize C++ code for a target platform.

## Prerequisites

Before starting, check if a platform configuration exists:
- Read `cpp-perf-platform.yaml` in the project root
- If it does not exist, ask the user to configure their target platform (see Platform Setup section at the end)
- Load the corresponding profile from `skills/cpp-perf/profiles/<profile-name>.yaml`

## Stage 1: Input Parsing

Identify the input mode and extract the target code.

**Step 1 — Detect input mode:**

| Signal | Mode | Action |
|--------|------|--------|
| User pastes code between backticks or says "this code" | **snippet** | Extract the code block directly |
| User mentions PR, commit, diff, or pastes unified diff format | **diff** | Parse diff to identify changed functions; use `Read` to get full source of each changed file |
| User says "optimize this file/function" or names a file path | **file-ref** | Use `Grep` to locate the function, `Read` to get the file |

**Step 2 — Expand context** (budget: up to 2 call-chain levels, max 30% context window):

1. Read `#include` directives from the target code
2. Use `Grep` to find definitions of types used in function signatures
3. Use `Grep` to find callers (1 level up) and callees (1 level down) of the target function
4. Stop expanding if approaching 30% of context window usage

**Step 3 — Detect build system:**

Use `Glob` to check for build files in the project root:
- `CMakeLists.txt` → CMake
- `BUILD` or `BUILD.bazel` → Bazel
- `meson.build` → Meson
- `Makefile` → Make

Record the build system type for Stage 4 (dependency handling).

**Step 4 — Confirm with user:**

Present a summary:
> "I've identified the target code: `function_name` in `file.cpp` (lines X-Y).
> Context loaded: N related files.
> Target platform: [from config].
> Proceeding to analysis. Say 'stop' at any point to pause."
```

- [ ] **Step 2: Verify markdown renders correctly**

Run: `wc -l skills/cpp-perf/cpp-perf.md`
Expected: file exists with content

- [ ] **Step 3: Commit**

```bash
git add skills/cpp-perf/cpp-perf.md
git commit -m "feat: add cpp-perf skill Stage 1 — input parsing"
```

---

### Task 8: Main Skill Instructions — Stage 2 (Static Analysis)

**Files:**
- Modify: `skills/cpp-perf/cpp-perf.md`

- [ ] **Step 1: Append Stage 2 to cpp-perf.md**

```markdown
## Stage 2: Static Analysis

Analyze the target code across four layers. Use the loaded platform profile to quantify estimates.

**Step 1 — Identify relevant layers:**

Scan the code and determine which layers apply:
- **Algorithm**: loops with O(n^2)+ patterns, linear search in large collections, redundant sorting
- **Language**: pass-by-value of large objects, missing `std::move`, `string` concatenation in loops, virtual calls in hot paths
- **Microarchitecture**: inner loops (vectorization candidates), conditional branches in hot paths, data dependency chains, AoS patterns with partial field access
- **System**: structs crossing cache lines, 2D array column-major access, potential false sharing in multithreaded code

**Step 2 — Consult knowledge base** (budget: up to 20% context window):

1. For each relevant layer, use `Glob` to list pattern files: `skills/cpp-perf/knowledge/patterns/<layer>/*.md`
2. **If no pattern files exist** (Plan 2 not yet implemented), skip this step and rely on LLM knowledge
3. If pattern files exist, read the frontmatter of each (first 10 lines) to check `keywords`
4. If any keyword matches a code characteristic, read the full pattern file
5. Use the pattern's Detection, Transformation, and Expected Impact sections to inform your analysis

**Step 3 — Consult library registry:**

1. Read `skills/cpp-perf/knowledge/libraries.yaml`
2. Scan the target code for standard library calls (`std::vector`, `std::sort`, `std::unordered_map`, `malloc`, math functions, etc.)
3. For each match, note the high-performance alternative, its `integration` level, and `advantage`

**Step 4 — Score each issue:**

For each identified issue, calculate an estimated performance impact:
- Use instruction latencies/throughputs from the platform profile
- For memory issues, use cache sizes and latencies
- Assign confidence level:
  - **HIGH**: pure instruction count math (e.g., scalar vs vector loop)
  - **MEDIUM**: involves cache behavior assumptions
  - **LOW**: depends on runtime data patterns

Collect all issues into a list sorted by estimated impact (highest first).
```

- [ ] **Step 2: Commit**

```bash
git add skills/cpp-perf/cpp-perf.md
git commit -m "feat: add cpp-perf skill Stage 2 — static analysis"
```

---

### Task 9: Main Skill Instructions — Stage 3 (Performance Report)

**Files:**
- Modify: `skills/cpp-perf/cpp-perf.md`

- [ ] **Step 1: Append Stage 3 to cpp-perf.md**

```markdown
## Stage 3: Performance Report & User Decision

Present findings as a graded report.

**Output format:**

Use exactly this structure:

---

## Performance Analysis Report

**Target:** `<function_name>` in `<file_path>`
**Platform:** <profile name> (<arch>)
**Analysis date:** <date>

### High Impact (estimated >20% improvement)

N. [PN] <title> — <file>:<line>
   - Current: <description>, estimated <X> cycles/iter
   - After optimization: <description>, estimated <Y> cycles/iter
   - Confidence: <HIGH|MEDIUM|LOW> (<reason>)
   - Dependencies: <any prerequisites>

### Medium Impact (estimated 5-20%)
...

### Low Impact (estimated <5%)
...

### Library Alternatives
- `std::unordered_map` at <file>:<line> → consider `absl::flat_hash_map` (drop-in, ~2x faster lookup)
...

---

**After presenting the report, ask:**

> "Which items do you want to optimize? Enter numbers (e.g., 1,2), or 'all'.
> Enter 'stop' if you only needed the report."

If user says 'stop' or indicates they only want the report, end here.
Otherwise, record selected items and proceed to Stage 4.
```

- [ ] **Step 2: Commit**

```bash
git add skills/cpp-perf/cpp-perf.md
git commit -m "feat: add cpp-perf skill Stage 3 — performance report"
```

---

### Task 10: Main Skill Instructions — Stage 4 (Benchmark, Compile & Execute)

**Files:**
- Modify: `skills/cpp-perf/cpp-perf.md`

- [ ] **Step 1: Append Stage 4 to cpp-perf.md**

```markdown
## Stage 4: Benchmark, Compile & Baseline Measurement

For each selected issue, generate a benchmark, compile, analyze assembly, and run on the target.

### 4a. Benchmark Generation

1. Read the benchmark template: `skills/cpp-perf/templates/benchmark.cpp.tmpl`
2. For each selected issue, fill in the template placeholders:

| Placeholder | Fill with |
|-------------|-----------|
| `{{INCLUDES}}` | Required headers for the target code |
| `{{SETUP_DATA}}` | `setup_data()` function that constructs test inputs |
| `{{IMPLEMENTATION}}` | The target function (copied or wrapped) |
| `{{FUNCTION_NAME}}` | Name of the function being benchmarked |
| `{{ISSUE_ID}}` | Issue identifier (e.g., P1) |
| `{{WARMUP_COUNT}}` | 1000 (default, adjust for very fast/slow functions) |
| `{{ITERATION_COUNT}}` | 10000 (default, adjust for very fast/slow functions) |
| `{{FUNCTION_CALL}}` | The actual call expression (e.g., `process(data)`) |
| `{{RESET_DATA}}` | Empty if function is pure; `data = setup_data();` if function mutates input |

3. **Construct test data** — follow this priority:

| Parameter type | Strategy |
|----------------|----------|
| `int`, `float`, `double`, etc. | Random values within typical range + boundary values |
| `std::vector<T>`, `std::array<T,N>` | Fill with random data, three sizes: small(16), medium(1024), large(65536) |
| `std::string` | Typical strings for the domain (ask user if unclear) |
| Custom struct/class | Read its definition, recursively construct each member |
| Pointer/reference | Heap-allocate the pointed-to object |
| Cannot determine | Ask the user with a specific question about typical values and sizes |

4. Write the filled benchmark to a temporary file (e.g., `/tmp/cpp-perf/benchmark_P1.cpp`)
5. Show the generated benchmark to the user and ask for confirmation before compiling

### 4b. Cross-Compilation

Read platform config from `cpp-perf-platform.yaml`:

```bash
<compiler> <compiler_flags> benchmark_P1.cpp -o benchmark_P1 -lm
```

If compilation fails:
1. Show the error
2. Attempt to fix (missing includes, type mismatches)
3. If unfixable, ask the user for help

If the compiler is GCC, add `-fopt-info-vec-missed` to get vectorization report.
If Clang, add `-Rpass-missed=loop-vectorize`.

### 4c. Disassembly Analysis

Derive the objdump command from the compiler path:
- `aarch64-linux-gnu-g++` → `aarch64-linux-gnu-objdump`
- `x86_64-linux-gnu-g++` → `x86_64-linux-gnu-objdump`
- General rule: replace `g++` or `gcc` with `objdump` in the compiler path

```bash
<objdump> -d benchmark_P1 | # extract target function section
```

Analyze the disassembly for:

| Check | Look for | Good sign | Bad sign |
|-------|----------|-----------|----------|
| Vectorization | NEON: `ld1`,`st1`,`fmul` / AVX: `vmulps`,`vaddps` | Vector instructions in inner loop | Only scalar instructions |
| Loop unrolling | Multiple copies of loop body | 2-8x unrolled | No unrolling on hot loop |
| Instruction selection | `fmla`/`vfmadd` (fused multiply-add) | FMA used | Separate mul + add |
| Register pressure | `str`/`ldr` to stack (`[sp, #offset]`) | Few stack spills | Many spills = too many live values |
| Memory pattern | Sequential `ldr` with incrementing offsets | Stride-1 access | Scattered offsets |

Present the disassembly findings. If the disassembly **contradicts** the static analysis (e.g., compiler already vectorized), **retract that issue** and inform the user.

### 4d. Remote Execution

Read SSH config from `cpp-perf-platform.yaml`. Build the SSH command:

```bash
# Upload
scp -P <port> [-i <key>] [-J <proxy>] benchmark_P1 <user>@<host>:<work_dir>/

# Execute
ssh -p <port> [-i <key>] [-J <proxy>] <user>@<host> "<work_dir>/benchmark_P1"
```

On first SSH command in this session, show the command to the user and ask for confirmation.

Parse the JSON output from stdout.

**Error handling:**
- SSH fails → show error, suggest `ssh -p <port> <user>@<host>` for manual test
- Binary crashes → show stderr, likely test data issue, ask user
- High stddev (>10%) → auto re-run with 2x iterations

### 4e. Baseline Data Analysis

Present results:

> **Baseline Measurement — [P1] <title>**
> - median: Xns, p99: Yns, stddev: Zns (W% of median)
> - Static analysis estimated: ~Ans → actual: Bns
> - [Match/Deviation explanation if >2x difference]

If results deviate significantly from static estimates, explain possible causes:
- Compiler already partially optimized
- Cache effects not accounted for
- Data-dependent behavior
```

- [ ] **Step 2: Commit**

```bash
git add skills/cpp-perf/cpp-perf.md
git commit -m "feat: add cpp-perf skill Stage 4 — benchmark, compile & execute"
```

---

### Task 11: Main Skill Instructions — Stages 5-6 (Optimize & Iterate)

**Files:**
- Modify: `skills/cpp-perf/cpp-perf.md`

- [ ] **Step 1: Append Stages 5-6 to cpp-perf.md**

```markdown
## Stage 5: Optimize, Verify & Compare

### 5a. Generate Optimized Code

For each selected issue with confirmed optimization opportunity (not retracted in 4c):

1. Generate the optimized version of the code
2. Explain each change:
   - What was changed
   - Why it is faster (reference platform profile data: instruction latencies, cache sizes, etc.)
   - If a knowledge base pattern was used, cite the source

### 5b. Correctness Verification

1. Read the correctness template: `skills/cpp-perf/templates/correctness.cpp.tmpl`
2. Fill in both baseline and optimized implementations
3. Set `{{EPSILON}}` to `1e-6f` for float, `1e-12` for double (user can override)
4. Generate a correctness check that:
   - Runs both implementations with the same input
   - Compares outputs element-by-element
   - Reports first mismatch if any
5. Cross-compile and run on target
6. If correctness fails:
   - Report the mismatch
   - Fix the optimization
   - Re-verify until passing

### 5c. Compile, Disassemble & Execute Optimized Version

Same process as Stage 4 (4b → 4c → 4d), but with the optimized implementation:

1. Cross-compile the optimized benchmark
2. Disassemble — verify expected instructions are present:
   - If the optimization was vectorization, confirm NEON/AVX instructions
   - If the optimization was branch elimination, confirm cmov/csel
   - If expected instructions are missing, investigate and adjust
3. Upload and execute on target
4. Parse JSON results

### 5d. Comparison Report

Present the comparison:

---

## Optimization Result

**[PN] <title> — <file>:<line>**

| Metric | Baseline | Optimized |
|--------|----------|-----------|
| Median | Xns | Yns |
| P99 | Xns | Yns |
| Speedup | — | Z.ZZx |
| Correctness | — | PASSED |

**Changes:**
- <bullet list of specific code changes>

**Disassembly confirmation:**
- <key instruction differences>

---

After presenting results for all selected issues, ask:

> "Optimization complete.
> - Accept these changes? I'll show the final optimized code for you to integrate.
> - Try alternative approach for any item? (e.g., 'retry P1')
> - Optimize additional items from the original report? (e.g., 'add P3')"

## Stage 6: Iteration

If the user requests an alternative approach:

1. Analyze why the current optimization underperformed
2. Propose a different strategy (e.g., if SIMD intrinsics didn't help much, try loop restructuring; if data structure change was too invasive, try algorithmic improvement)
3. Return to Stage 5a with the new approach

If the user wants additional items:
1. Return to Stage 4 for the newly selected items
```

- [ ] **Step 2: Commit**

```bash
git add skills/cpp-perf/cpp-perf.md
git commit -m "feat: add cpp-perf skill Stages 5-6 — optimize, verify & iterate"
```

---

### Task 12: Main Skill Instructions — Platform Setup Section

**Files:**
- Modify: `skills/cpp-perf/cpp-perf.md`

- [ ] **Step 1: Append Platform Setup section to cpp-perf.md**

```markdown
## Platform Setup

If no `cpp-perf-platform.yaml` exists, guide the user through interactive setup.

**Ask these questions in order:**

1. "What is your target architecture? (aarch64 / x86_64)"
2. "What cross-compiler do you use? (e.g., aarch64-linux-gnu-g++, or 'local' if compiling natively)"
3. "What compiler flags should I use? (e.g., -O2 -march=armv8.2-a)"
4. "Do you have a sysroot path for cross-compilation? (optional, e.g., /opt/arm-sysroot)"
5. "What is the SSH address of your target board? (e.g., user@192.168.1.100)"
   - Also ask for port if non-standard
   - Ask about SSH key path if not using ssh-agent
   - Ask about jump host if needed
6. "Which performance profile matches your target? Available profiles:"
   - List files in `skills/cpp-perf/profiles/` directory
   - If none match: "You can add a custom profile YAML or use the profiler to generate one (see Plan 3)"

**Generate `cpp-perf-platform.yaml`:**

```yaml
platforms:
  <board-name>:
    compiler: <answer-2>
    compiler_flags: "<answer-3>"
    sysroot: <answer-4, omit if none>
    host: <host-from-answer-5>
    port: <port, default 22>
    user: <user-from-answer-5>
    key: <key-path, omit if using ssh-agent>
    proxy: <jump-host, omit if none>
    arch: <answer-1>
    work_dir: /tmp/cpp-perf
    profile: <answer-6>
```

Write the file to the project root and confirm with the user.

**Verify connectivity:**

```bash
ssh -p <port> [-i <key>] <user>@<host> "echo 'cpp-perf connection OK' && uname -m"
```

If this succeeds, platform setup is complete.
```

- [ ] **Step 2: Commit**

```bash
git add skills/cpp-perf/cpp-perf.md
git commit -m "feat: add cpp-perf skill platform setup section"
```

---

### Task 13: End-to-End Manual Test

No automated tests for a Claude Code skill — the test is using it.

- [ ] **Step 1: Validate assembled cpp-perf.md**

Read the complete `skills/cpp-perf/cpp-perf.md` end-to-end. Verify:
- All 6 stages are present and properly structured
- All cross-references are correct (template paths, profile paths, libraries.yaml path)
- Markdown formatting is clean (no broken headings, unclosed code blocks)
- Platform Setup section is at the end

Fix any issues found.

- [ ] **Step 2: Create a test C++ file to optimize**

Create at `/tmp/cpp-perf/test_target.cpp`:

```cpp
// test_target.cpp — test file with deliberate performance issues
#include <vector>
#include <cmath>

// Issue: not vectorized, scalar processing
void scale_array(float* dst, const float* src, float factor, int n) {
    for (int i = 0; i < n; i++) {
        dst[i] = src[i] * factor;
    }
}

// Issue: unnecessary copy, pass by value
double sum_vector(std::vector<double> data) {
    double sum = 0;
    for (size_t i = 0; i < data.size(); i++) {
        sum += data[i];
    }
    return sum;
}

// Issue: branch-heavy, unpredictable
int count_threshold(const int* data, int n, int threshold) {
    int count = 0;
    for (int i = 0; i < n; i++) {
        if (data[i] > threshold) {
            count++;
        }
    }
    return count;
}
```

- [ ] **Step 3: Verify the skill triggers correctly (Stages 1-3)**

Invoke the skill by saying: "Optimize the performance of /tmp/cpp-perf/test_target.cpp for ARM Cortex-A78"

Verify:
- Skill activates (SKILL.md trigger matches)
- Stage 1 parses the file correctly, identifies three functions
- Stage 2 identifies at least the three deliberate issues (vectorization, copy, branching)
- Stage 3 produces a graded report with correct format (High/Medium/Low sections)

- [ ] **Step 4: Verify benchmark generation and compilation (Stage 4)**

Select one issue from the report (e.g., P1 scale_array) and proceed through Stage 4.
Verify:
- Benchmark code is generated from the template
- Cross-compilation command is constructed correctly from platform config
- Disassembly analysis section runs (if cross-compiler available)
- Remote execution produces JSON output (if target board available)

If no cross-compiler or target board available, verify the generated commands are correct even if they can't execute.

- [ ] **Step 5: Verify optimization and comparison (Stages 5-6)**

If target board is available, continue with Stage 5 for the selected issue:
- Verify optimized code is generated with explanation
- Verify correctness check runs (correctness template is used)
- Verify comparison report format (baseline vs optimized with speedup)
- Test iteration: request "try a different approach" and verify Stage 6 loops back

If target board is not available, verify the skill produces the correct optimized code and explains the changes, even without remote execution.

- [ ] **Step 6: Clean up test file**

```bash
rm -f /tmp/cpp-perf/test_target.cpp
```

- [ ] **Step 7: Final commit**

```bash
git add skills/cpp-perf/
git commit -m "feat: cpp-perf core skill complete — Plan 1 done"
```
