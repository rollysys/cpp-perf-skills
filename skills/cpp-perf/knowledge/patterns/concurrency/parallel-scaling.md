---
name: Parallel Scaling Optimization
source: perf-book Ch.13 (Optimizing Multithreaded Applications)
layers: [algorithm, system, microarchitecture]
platforms: [arm, x86]
keywords: [Amdahl, scaling, threads, speedup, serial fraction, load balancing, work stealing, USL, frequency throttling, oversubscription, memory bandwidth, thread pool, SMT]
---

## Problem

Multithreaded applications routinely fail to achieve linear speedup. The perf-book Ch.13 case study on an i7-1260P (4P+8E cores, 16 threads) quantifies real-world scaling:

| Benchmark    | Max speedup (16 threads) | Efficiency | Limiting factor |
|-------------|--------------------------|-----------|-----------------|
| Blender     | 6.1x                    | 38%       | Frequency throttling, E-core asymmetry, SMT limits |
| Clang build | ~3.4x (peaks at 10T)    | 21%       | Frequency throttling, I-cache/D-cache misses |
| Zstd        | ~3x (peaks at 5T)       | 19%       | Producer-consumer serialization, buffer pool contention |
| CloverLeaf  | ~2.5x (peaks at 3T)     | 16%       | Memory bandwidth saturation |
| CPython     | 1.0x (flat)             | 6%        | Global Interpreter Lock (GIL) |

Three theoretical frameworks explain these limits:

1. **Amdahl's Law**: `Speedup = 1 / (S + (1-S)/N)` where S is the serial fraction and N is thread count. A program 75% parallel converges to 4x speedup no matter how many cores are added.

2. **Universal Scalability Law (USL)**: extends Amdahl by modeling contention (threads competing for shared resources) and coherence (cost of maintaining consistent state). USL explains *retrograde* speedup -- performance that degrades beyond a critical thread count. The Clang and Zstd benchmarks exhibit this behavior.

3. **Frequency throttling**: CPUs reduce clock speed as more cores become active to stay within thermal/power limits. The perf-book measured P-core frequency dropping from ~4.7 GHz (single-core turbo) to 3.2 GHz (all 16 threads active). Disabling Turbo Boost doubled scaling efficiency: Blender 38% -> 69%, Clang 21% -> 41%.

SPEC CPU 2017 rate benchmarks (independent single-threaded instances, no synchronization) show only 40-70% scaling for integer workloads and 20-65% for FP workloads. These are *hardware-only* inefficiencies. Thread synchronization in real multithreaded programs degrades scaling further.

## Detection

**Thread count scaling study (the single most valuable analysis):**

```bash
# Run with increasing thread counts and measure wall-clock time
for t in 1 2 4 8 12 16; do
  echo "=== Threads: $t ==="
  /usr/bin/time -v ./my_app --threads=$t 2>&1 | grep "wall clock"
done
# Plot speedup = T(1) / T(N) vs. N
# Compare against linear (ideal) and Amdahl's law prediction
```

**Identify which scaling law applies:**
- Curve matches Amdahl closely -> serial code is the bottleneck
- Curve shows retrograde behavior (speedup *decreases* after a peak) -> USL: contention or coherence dominates
- Curve plateaus early regardless of serial fraction -> shared resource saturation (bandwidth, L3, I/O)
- Curve shape changes when Turbo Boost is disabled -> frequency throttling is a major factor

**Effective CPU Utilization:**

`Effective CPU Utilization = sum(Effective CPU Time per thread) / (elapsed_time x thread_count)`

where `Effective CPU Time = CPU Time - (Overhead Time + Spin Time)`. A thread with 100% CPU utilization but high spin time is wasting cycles on busy-waiting, not doing useful work. Intel VTune provides this metric directly.

**Key metrics to collect:**

```bash
# Context switches and migrations (oversubscription indicator)
perf stat -e context-switches,cpu-migrations -- ./my_app

# Memory bandwidth saturation (CloverLeaf pattern)
# TMA: Memory_Bound > DRAM_Memory_Bandwidth
perf stat -e offcore_response.all_data_rd.any_response -- ./my_app

# CPU frequency under load (throttling detection)
# Monitor with turbostat, VTune platform view, or:
perf stat -e msr/tsc/,msr/aperf/ -- ./my_app
```

**SMT scaling measurement:**
Divide performance of 2 threads on 1 core (2T1C) by 1 thread on 1 core (1T1C). The perf-book measured: Blender SMT scaling = 1.3x, Clang SMT scaling = 1.1x. Compute-bound SIMD workloads benefit least from SMT because sibling threads compete for the limited FP/SIMD execution units.

## Transformation

### Strategy 1: Isolate frequency throttling from software scaling

Disable Turbo Boost to measure "true" software scaling, then address thermal issues separately:

```bash
# x86 Linux: disable Turbo Boost
echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo
# Re-run scaling study
# Re-enable:
echo 0 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo

# ARM Linux: check governor and frequency limits
cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq
```

The perf-book shows this doubled effective scaling for both Blender (38% -> 69%) and Clang (21% -> 41%). In production, the fix is hardware-level: better cooling, higher TDP processors, or accepting reduced per-core frequency as a trade-off.

### Strategy 2: Dynamic work partitioning for asymmetric cores

On hybrid processors (Intel P+E, ARM big.LITTLE), static equal-size partitioning creates load imbalance because fast cores finish first and sit idle:

```cpp
// BAD: static partitioning -- E-cores/LITTLE cores become the tail
#pragma omp for schedule(static)

// BAD: static with affinity -- prevents migration entirely
// OMP_PROC_BIND=true
// Perf-book: 864ms (worst result)

// GOOD: dynamic partitioning with tuned chunk size
#pragma omp for schedule(dynamic, N/128)
// Perf-book: 517ms (best result, 40% faster than affinity)
```

The perf-book case study results on i7-1260P (2P+2E cores enabled):

| Strategy                    | Latency (ms) | vs. best |
|-----------------------------|-------------|----------|
| Static + affinity (pinned)  | 864         | +67%     |
| Static (no affinity)        | 567         | +10%     |
| Dynamic, 4 chunks           | 570         | +10%     |
| Dynamic, 16 chunks          | 541         | +5%      |
| Dynamic, 128 chunks         | 517         | baseline |
| Dynamic, 1024 chunks        | 560         | +8%      |

Sweet spot: enough chunks for load balancing, not so many that scheduling overhead dominates.

### Strategy 3: Right-size thread count to avoid oversubscription

More threads than hardware threads causes context-switch overhead and cache thrashing:

```cpp
// Query available parallelism
unsigned int hw_threads = std::thread::hardware_concurrency();

// For compute-bound work: match hardware thread count
unsigned int workers = hw_threads;

// For I/O-bound work: can exceed hardware threads
// (threads spend most time blocked)
unsigned int workers = hw_threads * 2;  // tune empirically

// For memory-bandwidth-bound work (CloverLeaf pattern):
// Fewer threads may be optimal!
// Perf-book: CloverLeaf saturates bandwidth at 3 threads on DDR4-2400
// Adding more threads provides zero benefit
```

Monitor Preemption Wait Time: if significant, you have oversubscription. The perf-book recommends "reducing the total number of threads or increasing task granularity for every worker thread."

### Strategy 4: Thread pools for short-lived tasks

Avoid repeated thread creation/destruction overhead:

```cpp
// BAD: thread-per-task
for (auto& task : tasks) {
  std::thread t(process, task);
  t.join();  // 10-50 us creation overhead per task
}

// GOOD: pre-allocated thread pool
ThreadPool pool(std::thread::hardware_concurrency());
for (auto& task : tasks)
  pool.enqueue(process, task);
pool.wait_all();
```

Thread creation costs 10-50 microseconds on Linux. For sub-millisecond tasks, this overhead dominates.

### Strategy 5: Address memory bandwidth saturation

When scaling stops due to bandwidth limits (the CloverLeaf pattern), software optimizations have diminishing returns:

```bash
# Confirm bandwidth saturation
# TMA Memory_Bound > 50% AND DRAM_Memory_Bandwidth > 80% = saturated
# Perf-book CloverLeaf: Memory_Bound rose from 34.6% (1T) to 65.4% (4T)
# DRAM_BW metric rose from 71.7% to 91.3%
```

Mitigations (in order of impact):
1. **Faster memory hardware**: DDR4-2400 -> DDR4-3200 gave 33% improvement for CloverLeaf at 16 threads (matching the bandwidth ratio 3200/2400 = 1.33)
2. **Reduce data footprint**: smaller types, compression, SoA layout
3. **Loop tiling**: maximize cache reuse before hitting DRAM
4. **NUMA-aware allocation** (multi-socket): `numactl --localalloc` to keep data on the accessing socket

Key insight from perf-book: "Having 16 active threads is enough to saturate two memory controllers even if CPU cores run at half the frequency. Since most of the time threads are just waiting for data, when you disable Turbo, they simply start to wait slower." Turbo Boost provides zero benefit for bandwidth-saturated workloads.

### Strategy 6: Identify the serial fraction with Coz

Traditional profilers show where time is spent, not where optimization effort matters. Coz uses "virtual speedups" to predict impact:

```bash
coz run --- ./my_app
# Example output: "improving line 540 by 20% -> 17% overall improvement"
# "improving line 540 by 45% -> impact levels off" (Amdahl ceiling)
```

This is critical for multithreaded programs where Amdahl's law makes intuition unreliable. A function consuming 30% of one thread's time might contribute 0% to overall latency if another thread is slower.

## Expected Impact

- **Dynamic vs. static scheduling** on asymmetric systems: 10-40% improvement (perf-book: 864ms -> 517ms, 40% gain)
- **Eliminating thread affinity** on hybrid processors: up to 35% (perf-book: 864ms -> 567ms)
- **Right-sizing thread count**: avoids retrograde speedup. The perf-book shows Clang and Zstd performance *decreasing* beyond 10 and 5 threads respectively.
- **Faster memory for bandwidth-bound workloads**: proportional to bandwidth increase (perf-book: 33% from DDR4-2400 -> DDR4-3200)
- **Thread pool reuse**: eliminates 10-50 us per-task overhead. For workloads with thousands of short tasks, this can be 2-5x improvement.
- **Addressing frequency throttling**: the perf-book shows this accounts for roughly half of unrealized scaling on consumer hardware

## Caveats

- **Frequency throttling is platform-specific**: a laptop throttles much harder than a server with liquid cooling. The perf-book's i7-1260P results (P-core dropping from 4.7 to 3.2 GHz) represent a consumer platform; server platforms with higher TDP and better cooling throttle less.
- **SIMD workloads hit lower frequencies**: AVX-512 causes additional frequency reduction beyond base throttling. Blender (heavy SIMD) may throttle more than Clang (scalar-dominant) even at the same thread count.
- **Dynamic scheduling has overhead**: each chunk dispatch involves synchronization. The perf-book shows 1024 chunks was *slower* than 128 chunks due to excessive management overhead. Tune empirically.
- **Bandwidth saturation is a hard wall**: no software optimization can exceed the physical DRAM bandwidth limit. The only remedies are hardware upgrades or algorithmic restructuring to reduce data movement.
- **SMT provides diminishing returns**: perf-book measured only 1.1-1.3x SMT scaling. For SIMD-heavy compute, two sibling threads compete for the same FP execution units.
- **Hybrid scheduling is OS-dependent**: Intel Thread Director requires Linux 5.18+ or Windows 11. macOS does not provide thread-to-core affinity APIs. ARM big.LITTLE scheduling depends on Energy Aware Scheduling (EAS) in the Linux kernel.
- **Oversubscription detection requires OS-level metrics**: Preemption Wait Time is not directly visible from user-space counters. Use VTune, `perf sched`, or `/proc/[pid]/status` voluntary/nonvoluntary context switch counters.
