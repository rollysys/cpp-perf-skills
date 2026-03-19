# cpp-perf Platform Profiler — Implementation Plan (Plan 3 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a cross-compilable C++ profiler that measures hardware characteristics (instruction latency, cache hierarchy, branch prediction, OS overhead) on a target platform and outputs a profile YAML compatible with the cpp-perf skill.

**Architecture:** Modular measurement framework. `main.cpp` dispatches to `measure_*.cpp` modules, each measuring one category. `output.cpp` collects all results and emits YAML to stdout. All measurements use cycle-counting or `steady_clock` timing with statistical analysis (median of multiple runs).

**Tech Stack:** C++17, pthreads, Linux syscalls, CMake

**Spec:** `docs/superpowers/specs/2026-03-19-cpp-perf-skill-design.md` (Platform Profiler section)

**Note:** This profiler targets Linux (ARM and x86). Some measurements (futex, eventfd, madvise) are Linux-specific. macOS support is out of scope for initial release.

---

## File Structure

```
skills/cpp-perf/profiler/
  CMakeLists.txt
  main.cpp                # Entry point + CLI
  common.h                # Shared timing utilities, statistics, result collection
  output.cpp              # YAML output generation
  measure_compute.cpp     # Instruction latency & throughput
  measure_cache.cpp       # Cache sizes & latency (pointer chasing)
  measure_memory.cpp      # Memory bandwidth, TLB
  measure_branch.cpp      # Branch misprediction penalty
  measure_os.cpp          # Syscall, thread, fork, CPU migration, synchronization
  measure_alloc.cpp       # malloc/free, mmap, page faults
  measure_io.cpp          # File I/O: open/close, read/write, fsync
  measure_ipc.cpp         # Pipe, eventfd, signal delivery, scheduling
```

---

### Task 1: Framework — CMakeLists.txt, common.h, main.cpp, output.cpp

**Files:**
- Create: `skills/cpp-perf/profiler/CMakeLists.txt`
- Create: `skills/cpp-perf/profiler/common.h`
- Create: `skills/cpp-perf/profiler/main.cpp`
- Create: `skills/cpp-perf/profiler/output.cpp`

- [ ] **Step 1: Create CMakeLists.txt**

```cmake
cmake_minimum_required(VERSION 3.10)
project(cpp-perf-profiler LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(profiler
    main.cpp
    output.cpp
    measure_compute.cpp
    measure_cache.cpp
    measure_memory.cpp
    measure_branch.cpp
    measure_os.cpp
    measure_alloc.cpp
    measure_io.cpp
    measure_ipc.cpp
)

target_link_libraries(profiler PRIVATE pthread)

# Cross-compilation: use toolchain file
# cmake -DCMAKE_TOOLCHAIN_FILE=aarch64-toolchain.cmake ..
```

- [ ] **Step 2: Create common.h**

Shared utilities: cycle timer, statistics (median, mean, stddev), result storage, and the measurement registration interface.

```cpp
#pragma once
#include <chrono>
#include <vector>
#include <string>
#include <map>
#include <algorithm>
#include <cmath>
#include <functional>
#include <cstdio>

namespace profiler {

// ============================================================
// Timing
// ============================================================
using Clock = std::chrono::steady_clock;
using ns = std::chrono::nanoseconds;

inline long long now_ns() {
    return std::chrono::duration_cast<ns>(Clock::now().time_since_epoch()).count();
}

// Cycle counter (platform-specific)
#if defined(__aarch64__)
inline uint64_t rdcycle() {
    uint64_t val;
    asm volatile("mrs %0, cntvct_el0" : "=r"(val));
    return val;
}
inline uint64_t cycle_freq() {
    uint64_t freq;
    asm volatile("mrs %0, cntfrq_el0" : "=r"(freq));
    return freq;
}
#elif defined(__x86_64__)
inline uint64_t rdcycle() {
    uint32_t lo, hi;
    asm volatile("rdtsc" : "=a"(lo), "=d"(hi));
    return ((uint64_t)hi << 32) | lo;
}
inline uint64_t cycle_freq() {
    // TSC frequency needs calibration; approximate with steady_clock
    auto t0 = Clock::now();
    uint64_t c0 = rdcycle();
    // Spin for ~10ms
    while (std::chrono::duration_cast<ns>(Clock::now() - t0).count() < 10000000) {}
    uint64_t c1 = rdcycle();
    auto t1 = Clock::now();
    double elapsed_s = std::chrono::duration_cast<ns>(t1 - t0).count() / 1e9;
    return (uint64_t)((c1 - c0) / elapsed_s);
}
#else
inline uint64_t rdcycle() { return 0; }
inline uint64_t cycle_freq() { return 1; }
#endif

// ============================================================
// Statistics
// ============================================================
struct Stats {
    double min, median, mean, p99, stddev;
};

inline Stats compute_stats(std::vector<double>& samples) {
    std::sort(samples.begin(), samples.end());
    Stats s;
    s.min = samples.front();
    s.median = samples[samples.size() / 2];
    s.mean = 0;
    for (auto v : samples) s.mean += v;
    s.mean /= samples.size();
    s.p99 = samples[(size_t)(samples.size() * 0.99)];
    double var = 0;
    for (auto v : samples) var += (v - s.mean) * (v - s.mean);
    s.stddev = std::sqrt(var / samples.size());
    return s;
}

// ============================================================
// Results collection
// ============================================================
// Hierarchical map: section -> key -> value (in cycles)
using ResultMap = std::map<std::string, std::map<std::string, double>>;

// Global results
inline ResultMap& results() {
    static ResultMap r;
    return r;
}

inline void record(const std::string& section, const std::string& key, double cycles) {
    results()[section][key] = cycles;
}

// ============================================================
// Measurement helpers
// ============================================================
// Run a function N times, collect timing, return median cycles
template <typename Fn>
double measure_cycles(Fn fn, int iterations = 1000, int warmup = 100) {
    for (int i = 0; i < warmup; i++) fn();

    std::vector<double> samples;
    samples.reserve(iterations);
    double freq = (double)cycle_freq();

    for (int i = 0; i < iterations; i++) {
        uint64_t c0 = rdcycle();
        fn();
        uint64_t c1 = rdcycle();
        samples.push_back((double)(c1 - c0));
    }

    auto stats = compute_stats(samples);
    return stats.median;
}

// Measure cycles per operation when fn performs `ops_per_call` operations
template <typename Fn>
double measure_cycles_per_op(Fn fn, int ops_per_call, int iterations = 1000, int warmup = 100) {
    return measure_cycles(fn, iterations, warmup) / ops_per_call;
}

// Prevent compiler from optimizing away a value
template <typename T>
inline void escape(T const& val) {
    asm volatile("" : : "r,m"(val) : "memory");
}

// Prevent reordering across this point
inline void clobber() {
    asm volatile("" ::: "memory");
}

// ============================================================
// Measurement module interface
// ============================================================
void measure_compute();
void measure_cache();
void measure_memory();
void measure_branch();
void measure_os();
void measure_alloc();
void measure_io();
void measure_ipc();
void output_yaml();

} // namespace profiler
```

- [ ] **Step 3: Create main.cpp**

```cpp
#include "common.h"
#include <cstring>

int main(int argc, char* argv[]) {
    bool run_all = (argc == 1);

    auto should_run = [&](const char* name) {
        if (run_all) return true;
        for (int i = 1; i < argc; i++)
            if (strcmp(argv[i], name) == 0) return true;
        return false;
    };

    fprintf(stderr, "cpp-perf profiler starting...\n");

    if (should_run("compute"))  { fprintf(stderr, "[compute] measuring...\n");  profiler::measure_compute(); }
    if (should_run("cache"))    { fprintf(stderr, "[cache] measuring...\n");    profiler::measure_cache(); }
    if (should_run("memory"))   { fprintf(stderr, "[memory] measuring...\n");   profiler::measure_memory(); }
    if (should_run("branch"))   { fprintf(stderr, "[branch] measuring...\n");   profiler::measure_branch(); }
    if (should_run("os"))       { fprintf(stderr, "[os] measuring...\n");       profiler::measure_os(); }
    if (should_run("alloc"))    { fprintf(stderr, "[alloc] measuring...\n");    profiler::measure_alloc(); }
    if (should_run("io"))       { fprintf(stderr, "[io] measuring...\n");       profiler::measure_io(); }
    if (should_run("ipc"))      { fprintf(stderr, "[ipc] measuring...\n");      profiler::measure_ipc(); }

    fprintf(stderr, "done. outputting YAML...\n");
    profiler::output_yaml();
    return 0;
}
```

- [ ] **Step 4: Create output.cpp**

Reads the global `results()` map and emits YAML to stdout matching the profile schema.

```cpp
#include "common.h"

namespace profiler {

void output_yaml() {
    auto& r = results();

    printf("# Auto-generated by cpp-perf profiler\n");

    // Emit each section
    for (auto& [section, kvs] : r) {
        printf("\n%s:\n", section.c_str());
        for (auto& [key, val] : kvs) {
            if (val == (int)val)
                printf("  %s: %d\n", key.c_str(), (int)val);
            else
                printf("  %s: %.1f\n", key.c_str(), val);
        }
    }
}

} // namespace profiler
```

- [ ] **Step 5: Create stub measure_*.cpp files**

Create 8 stub files that just define the function with a TODO comment. This allows CMake to build immediately. Each will be filled in subsequent tasks.

Each stub:
```cpp
#include "common.h"
namespace profiler {
void measure_XXX() {
    // TODO: implement in Task N
}
} // namespace profiler
```

- [ ] **Step 6: Verify build**

```bash
cd skills/cpp-perf/profiler && mkdir -p build && cd build && cmake .. && make -j4
```

Expected: compiles successfully, runs with no measurements, outputs empty YAML.

- [ ] **Step 7: Commit**

```bash
git add skills/cpp-perf/profiler/
git commit -m "feat: add profiler framework — CMake, timing utils, YAML output, stubs"
```

---

### Task 2: measure_compute.cpp — Instruction Latency & Throughput

**Files:**
- Modify: `skills/cpp-perf/profiler/measure_compute.cpp`

- [ ] **Step 1: Implement instruction latency measurements**

Use dependent instruction chains to measure latency. Each chain forces serial execution through data dependencies.

Measure: integer add, mul, div; FP fadd, fmul, fdiv; NEON vmul, fmla (ARM) or AVX vmulps, vfmadd (x86).

Use inline asm with platform-specific instructions. Chain length of 100+ iterations to amortize overhead.

- [ ] **Step 2: Implement instruction throughput measurements**

Use independent instruction streams to saturate execution units.

- [ ] **Step 3: Record results**

```cpp
record("instructions.integer", "add", lat_add);    // lat field
// ... etc
```

- [ ] **Step 4: Test build**

- [ ] **Step 5: Commit**

```bash
git add skills/cpp-perf/profiler/measure_compute.cpp
git commit -m "feat: add instruction latency and throughput measurements"
```

---

### Task 3: measure_cache.cpp — Cache Hierarchy

**Files:**
- Modify: `skills/cpp-perf/profiler/measure_cache.cpp`

- [ ] **Step 1: Implement pointer-chasing latency measurement**

Allocate arrays of increasing sizes (4KB to 64MB). Create a random pointer-chase pattern within each array. Measure access latency. Detect latency jumps to identify L1/L2/L3 boundaries and their latencies.

- [ ] **Step 2: Detect cache line size**

Access with varying strides (1, 2, 4, 8, 16, 32, 64, 128 bytes). Latency plateaus when stride >= cache line size.

- [ ] **Step 3: Record results**

```cpp
record("cache.l1d", "size_kb", detected_l1_size / 1024);
record("cache.l1d", "latency", l1_latency_cycles);
record("cache.l1d", "line_bytes", line_size);
// ... l2, l3
```

- [ ] **Step 4: Test and commit**

```bash
git add skills/cpp-perf/profiler/measure_cache.cpp
git commit -m "feat: add cache hierarchy detection via pointer chasing"
```

---

### Task 4: measure_memory.cpp — Bandwidth & TLB

**Files:**
- Modify: `skills/cpp-perf/profiler/measure_memory.cpp`

- [ ] **Step 1: Implement bandwidth measurement**

Sequential read/write of large arrays (larger than LLC). Measure bytes/second, convert to bytes/cycle using cycle counter.

- [ ] **Step 2: Implement TLB miss penalty measurement**

Access pages with stride = page_size, measure latency difference vs stride < page_size.

- [ ] **Step 3: Record results and commit**

```bash
git add skills/cpp-perf/profiler/measure_memory.cpp
git commit -m "feat: add memory bandwidth and TLB measurements"
```

---

### Task 5: measure_branch.cpp — Branch Misprediction

**Files:**
- Modify: `skills/cpp-perf/profiler/measure_branch.cpp`

- [ ] **Step 1: Implement branch misprediction penalty measurement**

Compare predictable branches (always-taken) vs random branches (50/50). The delta is the misprediction penalty.

Use a pre-generated array of random booleans to avoid RNG in the hot loop.

- [ ] **Step 2: Record results and commit**

```bash
git add skills/cpp-perf/profiler/measure_branch.cpp
git commit -m "feat: add branch misprediction penalty measurement"
```

---

### Task 6: measure_os.cpp — Process, Thread, Synchronization

**Files:**
- Modify: `skills/cpp-perf/profiler/measure_os.cpp`

- [ ] **Step 1: Implement process/thread measurements**

- syscall: repeated `getpid()`
- thread_create: `pthread_create` + `pthread_join`
- fork: `fork` + `_exit` + `waitpid`
- cpu_migration: set affinity to core 0, then core 1, measure migration cost

- [ ] **Step 2: Implement synchronization measurements**

- mutex: uncontended `pthread_mutex_lock/unlock`
- spinlock: custom `atomic_flag` spinlock, uncontended
- rwlock: uncontended `pthread_rwlock_rdlock/unlock` and `wrlock/unlock`
- futex: `FUTEX_WAKE`/`FUTEX_WAIT` round-trip (Linux only)
- atomic: `std::atomic<int>` with `seq_cst`, `acq_rel`, `relaxed`

- [ ] **Step 3: Record results**

```cpp
record("os_overhead", "syscall", syscall_cycles);
record("os_overhead", "thread_create", thread_cycles);
record("os_overhead", "mutex_lock_unlock", mutex_cycles);
record("os_overhead", "atomic_seq_cst", atomic_sc_cycles);
// ... etc
```

- [ ] **Step 4: Commit**

```bash
git add skills/cpp-perf/profiler/measure_os.cpp
git commit -m "feat: add OS overhead measurements — syscall, thread, sync"
```

---

### Task 7: measure_alloc.cpp — Memory Allocation

**Files:**
- Modify: `skills/cpp-perf/profiler/measure_alloc.cpp`

- [ ] **Step 1: Implement malloc/free measurements**

Measure malloc+free cycle for sizes: 16B, 256B, 4KB, 1MB.

- [ ] **Step 2: Implement mmap/page fault measurements**

- mmap_anon: `mmap(MAP_ANONYMOUS|MAP_PRIVATE)` + `munmap`
- minor_page_fault: access freshly mmap'd page
- major_page_fault: `madvise(MADV_DONTNEED)` then re-access
- huge_page_alloc: `mmap(MAP_HUGETLB)` if available

- [ ] **Step 3: Record results and commit**

```bash
git add skills/cpp-perf/profiler/measure_alloc.cpp
git commit -m "feat: add memory allocation measurements"
```

---

### Task 8: measure_io.cpp — File I/O

**Files:**
- Modify: `skills/cpp-perf/profiler/measure_io.cpp`

- [ ] **Step 1: Implement file I/O measurements**

All on /tmp (tmpfs when available) to isolate from disk:
- file_open_close: repeated `open` + `close`
- read_4kb: `read` of 4KB from pre-opened file
- write_4kb: `write` of 4KB to pre-opened file
- fsync: `write` + `fsync`

- [ ] **Step 2: Record results and commit**

```bash
git add skills/cpp-perf/profiler/measure_io.cpp
git commit -m "feat: add file I/O measurements"
```

---

### Task 9: measure_ipc.cpp — IPC & Scheduling

**Files:**
- Modify: `skills/cpp-perf/profiler/measure_ipc.cpp`

- [ ] **Step 1: Implement IPC measurements**

- pipe_roundtrip: `pipe` write+read of 64 bytes between threads
- eventfd_roundtrip: `eventfd_write` + `eventfd_read`
- signal_delivery: `kill(getpid(), SIGUSR1)` + handler return

- [ ] **Step 2: Implement scheduling measurements**

- sched_yield: `sched_yield()` round-trip
- timer_resolution_ns: successive `clock_gettime` delta
- context_switch: `pipe` ping-pong between two processes

- [ ] **Step 3: Record results and commit**

```bash
git add skills/cpp-perf/profiler/measure_ipc.cpp
git commit -m "feat: add IPC and scheduling measurements"
```

---

### Task 10: Integration & Output Formatting

- [ ] **Step 1: Update output.cpp for proper YAML structure**

The output must match the profile schema from the spec. Update `output_yaml()` to emit structured YAML with proper nesting (not flat key-value). The profiler populates the `cache`, `instructions`, `branch`, `memory_system`, and `os_overhead` sections. The `pipeline` and `registers` sections are looked up from CPU model (or omitted if unknown).

- [ ] **Step 2: Add CPU model detection**

Read `/proc/cpuinfo` (Linux) to detect CPU model. Match against a small lookup table of known microarchitectures to fill in `pipeline` and `registers` fields.

- [ ] **Step 3: Full build and test**

```bash
cd skills/cpp-perf/profiler/build && cmake .. && make -j4
./profiler  # should output valid YAML
```

Verify the output is valid YAML: pipe to `python3 -c "import yaml,sys; yaml.safe_load(sys.stdin)"`.

- [ ] **Step 4: Commit**

```bash
git add skills/cpp-perf/profiler/
git commit -m "feat: profiler complete — structured YAML output with CPU model detection"
```
