---
name: Threading Scalability Optimization
source: perf-book Ch.13
layers: [system, algorithm]
platforms: [arm, x86]
keywords: [thread, scalability, Amdahl, parallel, contention, lock, granularity, work stealing, load balancing, NUMA, affinity]
---

## Problem

Multithreaded applications frequently fail to achieve linear speedup with added cores. The perf-book Ch.13 case study shows real-world scaling results: Blender achieves only 6.1x on 16 threads (38% efficiency), Clang compilation peaks at ~10 threads then degrades, Zstd stalls at 5 threads, CloverLeaf saturates at 3 threads, and CPython shows zero scaling due to GIL. These are representative -- SPEC CPU 2017 rate benchmarks show only 20-70% scaling efficiency even without thread synchronization overhead.

Three fundamental laws govern the limits:

1. **Amdahl's Law**: speedup is bounded by `1 / (S + (1-S)/N)` where S is the serial fraction and N is thread count. A program that is 75% parallel converges to 4x speedup regardless of core count.
2. **Universal Scalability Law (USL)**: extends Amdahl by modeling contention (threads competing for shared resources) and coherence (cost of maintaining consistent state across caches). USL explains *retrograde* speedup -- performance that actually degrades beyond a critical thread count.
3. **Frequency throttling**: when multiple cores are active, CPUs reduce clock speeds to stay within thermal/power limits. The perf-book shows Clang's P-core frequency dropping from ~4.7 GHz (single core turbo) to 3.2 GHz with all 16 threads active. Disabling Turbo Boost doubled Blender's scaling efficiency from 38% to 69% by removing this variable.

## Detection

**Metrics to collect:**

- **Effective CPU Utilization**: `(sum of Effective CPU Time per thread) / (elapsed time x thread count)`. Effective CPU Time excludes overhead time (runtime library costs) and spin time (busy-waiting on locks). Intel VTune provides this directly.
- **Wait Time**: time threads spend blocked. Subdivides into *Sync Wait Time* (contended locks) and *Preemption Wait Time* (OS scheduler eviction, oversubscription).
- **Spin Time**: CPU-busy wait time from polling-based synchronization primitives. High spin time means lost opportunity for useful work.
- **Thread Count Scaling curve**: run the workload with 1, 2, 4, 8, ... N threads and plot speedup vs. thread count. Compare against linear scaling to identify the inflection point.

**Profile-level indicators:**

- `perf stat -e context-switches,cpu-migrations`: high values suggest excessive synchronization or oversubscription
- TMA: high `Memory_Bound > DRAM_Memory_Bandwidth` indicates shared memory bandwidth saturation (CloverLeaf pattern)
- Intel VTune Threading Analysis: reports Wait Time, Spin Time, and identifies hot locks with call stacks
- `perf record -g` on lock acquisition paths: identifies which code paths lead to contended locks
- Linux `perf sched` and `perf lock`: visualize scheduling and lock contention

**Characteristic symptoms:**
- Speedup curve flattens well before core count
- Adding threads causes *slower* execution (retrograde speedup from USL)
- Some threads idle while others work (load imbalance visible on timeline)
- High context-switch rate relative to useful work

## Transformation

### Strategy 1: Thread count scaling study (always do this first)

Run the application with 1, 2, 4, 8, ... N threads. Plot speedup curve. Compare with Amdahl's law prediction to estimate the serial fraction:

```bash
# Quick scaling study script
for t in 1 2 4 8 12 16; do
  echo "=== Threads: $t ==="
  time ./my_app --threads=$t
done
```

If the curve matches Amdahl closely, the bottleneck is serial code. If it shows retrograde behavior (USL pattern), contention or coherence is the dominant issue. If it plateaus early, suspect shared resource saturation (memory bandwidth, I/O, L3 cache).

### Strategy 2: Eliminate frequency throttling as a variable

Disable Turbo Boost and re-run the scaling study to isolate software-level scaling issues from thermal throttling:

```bash
# Linux: disable Turbo Boost
echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo
# Run scaling study again, then re-enable:
echo 0 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo
```

The perf-book shows this doubled effective scaling for both Blender (38% -> 69%) and Clang (21% -> 41%). In production, thermal solutions (better cooling, higher TDP processors) address this.

### Strategy 3: Dynamic work partitioning for asymmetric systems

On hybrid processors (Intel P+E cores, ARM big.LITTLE), static equal-size partitioning causes load imbalance because P-cores finish 2x faster than E-cores. Use dynamic scheduling:

```cpp
// BAD: static partitioning -- E-cores become the bottleneck
#pragma omp for schedule(static)

// GOOD: dynamic partitioning with appropriate chunk size
// Too few chunks -> load imbalance; too many -> scheduling overhead
#pragma omp for schedule(dynamic, N/128)
```

The perf-book case study shows concrete results on i7-1260P (4P+8E cores):
- Static with affinity: 864 ms (worst -- threads pinned to slow cores can't migrate)
- Static without affinity: 567 ms (OS migrates threads, but E-cores still idle early)
- Dynamic, 128 chunks: 517 ms (best -- fine-grained load balancing)
- Dynamic, 1024 chunks: 560 ms (too fine -- scheduling overhead dominates)

### Strategy 4: Avoid thread affinity on asymmetric systems

Do NOT pin threads to specific cores unless you have measured that it helps:

```cpp
// BAD on hybrid systems: prevents migration from E-core to idle P-core
// OMP_PROC_BIND=true
// pthread_setaffinity_np(...)
// sched_setaffinity(...)

// GOOD: let the OS scheduler handle placement
// Intel Thread Director on hybrid CPUs monitors real-time performance
// and migrates threads to optimal cores automatically
```

Thread affinity is only beneficial when: (a) all cores are symmetric, (b) you need deterministic NUMA-local allocation, or (c) you are doing latency-sensitive work on an isolated core.

### Strategy 5: Detect and resolve memory bandwidth saturation

When scaling stops due to bandwidth saturation (the CloverLeaf pattern), adding more threads provides no benefit -- threads just wait longer for data:

```bash
# Measure memory bandwidth utilization
perf stat -e offcore_response.all_data_rd.any_response \
          -e offcore_response.all_rfo.any_response -- ./my_app

# Or use Intel Memory Bandwidth Monitoring
sudo perf stat -e intel_cqm/llc_occupancy/ -- ./my_app
```

Mitigations:
- **Faster memory**: the perf-book shows upgrading DDR4-2400 to DDR4-3200 gave 33% improvement for CloverLeaf at 16 threads
- **Reduce data footprint**: smaller data types, compression, SoA layout to improve cache utilization
- **NUMA-aware allocation**: on multi-socket systems, allocate memory local to the socket that uses it
- **Loop tiling / blocking**: restructure access patterns to maximize cache reuse before hitting DRAM

### Strategy 6: Find and fix contended locks

Use timeline visualization and lock profiling to identify synchronization bottlenecks:

```bash
# Intel VTune Threading Analysis
vtune -collect threading -r results -- ./my_app

# Linux perf lock contention analysis
perf lock record -- ./my_app
perf lock report

# eBPF-based GAPP profiler (no recompilation needed)
# Tracks futex contention, ranks bottleneck criticality
# https://github.com/RN-dev-repo/GAPP/
```

The perf-book demonstrates using VTune's Bottom-up view to trace contended locks through call stacks. In the CPython case, this revealed `take_gil` -> `___pthread_cond_timedwait64` as the serialization point (GIL), with 5ms yield intervals causing alternating execution.

### Strategy 7: Use Coz profiler for causal analysis

Traditional profilers show where time is spent, not where optimization effort should go. Coz uses "virtual speedups" to predict the impact of optimizing specific code regions:

```bash
# Build with debug info and Coz support
coz run --- ./my_app
# Coz output: "improving line X by 20% would improve overall performance by 17%"
```

Coz inserts pauses to slow down all *other* code, simulating a speedup of the target region. It quantifies the potential impact before you invest optimization effort -- critical for multithreaded programs where Amdahl's law makes intuition unreliable.

### Strategy 8: Use thread pools for short-lived tasks

Avoid thread creation/destruction overhead for frequent short tasks:

```cpp
// BAD: creating threads per task
for (auto& task : tasks) {
  std::thread t(process, task);
  t.detach();  // creation overhead per task
}

// GOOD: thread pool with work queue
ThreadPool pool(std::thread::hardware_concurrency());
for (auto& task : tasks) {
  pool.enqueue(process, task);  // reuse existing threads
}
```

Thread creation costs 10-50 microseconds on Linux. For tasks shorter than a few milliseconds, pool overhead dominates.

### Strategy 9: Right-size thread count

Oversubscription (more threads than hardware threads) causes context-switch overhead and cache thrashing:

```cpp
// Query available parallelism
unsigned int hw_threads = std::thread::hardware_concurrency();

// For compute-bound work: match hardware thread count
unsigned int worker_count = hw_threads;

// For I/O-bound work: may exceed hardware threads
// (threads spend most time blocked on I/O)
unsigned int worker_count = hw_threads * 2;  // tune empirically
```

Monitor Preemption Wait Time -- if significant, you have oversubscription.

## Expected Impact

- **Fixing load imbalance** (static -> dynamic scheduling): 10-40% improvement on asymmetric systems (perf-book shows 864ms -> 517ms, a 40% improvement)
- **Removing affinity constraints**: up to 35% improvement on hybrid processors (864ms -> 567ms in the perf-book case)
- **Resolving bandwidth saturation**: limited by hardware -- faster memory can give up to 33% (perf-book DDR4-2400 -> DDR4-3200 result)
- **Eliminating serial bottlenecks**: depends on serial fraction. Reducing serial portion from 25% to 10% changes Amdahl limit from 4x to 10x
- **Overall**: thread count scaling studies are the single most valuable analysis for multithreaded performance. The perf-book states that advice from Ch.13 "may bring the most significant performance improvements" of anything in the book

## Caveats

- **Frequency throttling is hardware-specific**: scaling results differ dramatically between platforms. A laptop with passive cooling throttles much harder than a server with liquid cooling. Always measure on your target hardware.
- **Hybrid processor scheduling is OS-dependent**: Intel Thread Director works with Linux 5.18+ and Windows 11. Older kernels treat all cores equally, causing suboptimal placement. macOS does not expose thread-to-core affinity APIs at all.
- **Dynamic scheduling adds overhead**: each chunk dispatch involves synchronization. If chunks are too small (e.g., 1024 chunks in the perf-book example), scheduling overhead exceeds the load-balancing benefit.
- **Memory bandwidth is a hard wall**: no amount of software optimization can exceed the physical bandwidth limit. The only remedies are hardware upgrades, reducing data movement, or algorithmic changes to improve cache reuse.
- **Coz has limitations**: it requires programs to use `pthreads` or similar, does not work well with spin-locks, and its virtual speedup technique can be imprecise for very short code regions. It does not support Windows.
- **NUMA effects on multi-socket**: on multi-socket systems, memory accesses to a remote socket's DRAM cost 1.5-3x more than local accesses. Use `numactl --localalloc` or `libnuma` to ensure NUMA-local allocation. The perf-book's single-socket case study does not cover this, but it is critical for server workloads.
- **SMT provides diminishing returns**: the perf-book measures SMT scaling at only 1.1-1.3x (Clang: 1.1x, Blender: 1.3x). Two SMT threads share execution units, so compute-bound workloads gain very little from hyperthreading.
