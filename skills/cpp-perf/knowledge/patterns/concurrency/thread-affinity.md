---
name: Thread Affinity and Task Scheduling
source: perf-book Ch.13 (Optimizing Multithreaded Applications)
layers: [system, algorithm]
platforms: [arm, x86]
keywords: [affinity, NUMA, core pinning, sched_setaffinity, CPU migration, taskset, hybrid, big.LITTLE, P-core, E-core, OMP_PROC_BIND, thread director, dynamic scheduling, work stealing, asymmetric]
---

## Problem

Thread-to-core assignment (affinity) directly impacts performance on modern processors, especially hybrid architectures with heterogeneous cores. The perf-book Ch.13 demonstrates through concrete experiments that naive affinity decisions can cause severe performance degradation.

**Hybrid processor challenges:**

Modern CPUs increasingly use asymmetric core configurations:
- Intel Alder Lake / Meteor Lake: Performance (P) cores + Efficiency (E) cores. Meteor Lake adds a third core type.
- ARM big.LITTLE / DynamIQ: big (high-performance) + LITTLE (energy-efficient) cores.

The perf-book's case study on i7-1260P (4P+8E cores) shows P-cores process SIMD-heavy work roughly 2x faster than E-cores. This asymmetry creates three distinct problems:

1. **Pinning threads to cores blocks migration**: when threads are pinned to E-cores via affinity, they cannot migrate to idle P-cores. The perf-book measures this as the worst scheduling strategy (864ms vs. 517ms optimal -- a 67% penalty).

2. **Static equal partitioning wastes fast cores**: dividing work into N equal chunks (one per core) means P-cores finish early and idle while E-cores still process their equal-sized chunks.

3. **Fine-grained dynamic scheduling adds overhead**: splitting work into too many small chunks introduces scheduling overhead that negates the load-balancing benefit.

**The perf-book's scheduling experiments (i7-1260P, 2P+2E enabled):**

| Strategy | Latency | Notes |
|----------|---------|-------|
| Static + OMP_PROC_BIND=true | 864 ms | Threads pinned; E-cores can't migrate to idle P-cores |
| Static (no affinity) | 567 ms | OS migrates threads, but E-cores still idle early |
| Dynamic, 4 chunks | 570 ms | Same as static -- chunks too large for load balance |
| Dynamic, 16 chunks | 541 ms | Better balance, some idle time remains |
| Dynamic, 128 chunks | 517 ms | Sweet spot -- good balance, low overhead |
| Dynamic, 1024 chunks | 560 ms | Too fine -- scheduling overhead dominates |

**Frequency throttling compounds the problem:**

The perf-book shows that CPU frequency drops as more cores are utilized. On the i7-1260P, P-core frequency drops from ~4.7 GHz (single-core turbo) to 3.2 GHz (all 16 threads active), while E-cores operate at 2.6 GHz. The "tipping point" where adding threads hurts Clang performance is around 10 threads, where frequency throttling outweighs the benefit of additional cores.

Applications that use SIMD instructions may see even more aggressive throttling because SIMD execution units consume more power.

## Detection

**Symptoms of poor thread scheduling:**

1. Thread timeline shows idle gaps on fast cores while slow cores still process:
```bash
# VTune: visualize thread-to-core mapping over time
vtune -collect threading -r results -- ./my_app
# In timeline view: check if P-cores/big cores have idle periods
# while E-cores/LITTLE cores are still working
```

2. Scaling degrades earlier than expected:
```bash
# Scaling study
for t in 1 2 4 8 12 16; do
  echo "=== Threads: $t ==="
  /usr/bin/time ./my_app --threads=$t
done
# If speedup drops sharply when E-cores start being used
# (e.g., after 4 threads on 4P+8E system), asymmetry is the issue
```

3. Affinity is forcibly set in the code:
```bash
# Check for affinity calls in source
grep -rn "sched_setaffinity\|pthread_setaffinity\|CPU_SET\|OMP_PROC_BIND\|SetThreadAffinityMask" src/
# Check runtime environment
echo $OMP_PROC_BIND  # should be "false" or unset on hybrid systems
```

4. Frequency throttling causing retrograde scaling:
```bash
# Monitor per-core frequency under load
# x86:
sudo turbostat --interval 1 -- ./my_app
# or VTune platform view for per-core frequency chart

# ARM Linux:
cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq
```

**Key indicators:**
- Large wait time at barriers/joins (fast threads waiting for slow threads)
- Non-uniform core utilization in profiler timeline views
- Speedup curve inflection point correlating with E-core/LITTLE-core activation
- CPU frequency charts showing significant drops as thread count increases

## Transformation

### Strategy 1: Let the OS schedule threads (default recommendation)

The perf-book's general recommendation: "let the operating system do its job and not restrict it too much. The operating system knows how to schedule tasks to minimize contention, maximize reuse of data in caches, and ultimately maximize performance."

```cpp
// DO NOT set affinity on hybrid systems
// BAD:
// OMP_PROC_BIND=true
// sched_setaffinity(...)
// pthread_setaffinity_np(...)
// SetThreadAffinityMask(...)  (Windows)

// GOOD: let the OS + hardware schedulers handle it
// Intel Thread Director monitors real-time performance data
// and moves threads to the optimal core type automatically
// ARM EAS (Energy Aware Scheduling) does similar work
```

macOS does not provide thread-to-core affinity APIs at all -- the OS always controls placement.

### Strategy 2: Use dynamic work partitioning

Replace static equal-size chunks with dynamic scheduling:

```cpp
// BAD on asymmetric systems: equal chunks, P-cores finish first and idle
#pragma omp parallel for schedule(static)
for (int i = 0; i < N; i++)
  process(data[i]);

// GOOD: dynamic scheduling adapts to core speed differences
// Chunk size = total_work / (num_threads * factor)
// Factor of 8-32 typically works well
#pragma omp for schedule(dynamic, N / (omp_get_num_threads() * 16))
for (int i = 0; i < N; i++)
  process(data[i]);

// Or use guided scheduling (decreasing chunk sizes):
#pragma omp for schedule(guided)
// Starts with large chunks, reduces over time
// Good when later iterations are faster (e.g., triangular loops)
```

For non-OpenMP code, implement work-stealing:

```cpp
// Work-stealing queue pattern
// Each thread has a local deque of tasks
// When a thread's deque is empty, it "steals" from another thread's deque
// Libraries: Intel TBB (tbb::task_group), std::execution::par (C++17)

#include <execution>
std::for_each(std::execution::par,
              data.begin(), data.end(),
              [](auto& item) { process(item); });
// The standard library implementation handles work distribution
```

### Strategy 3: Core-type-aware scheduling (when you know the workload)

The perf-book provides guidance for specific workload types:

```
Compute-intensive, lightly threaded (e.g., compression):
  -> Schedule ONLY on P-cores / big cores

Background tasks (e.g., video calls):
  -> Schedule on E-cores / LITTLE cores to save power

Bursty, latency-sensitive (e.g., productivity apps):
  -> Use P-cores / big cores only

Sustained parallel throughput (e.g., video rendering):
  -> Use ALL cores with dynamic scheduling
```

On Linux with `cgroups` or `taskset`, you can restrict to specific cores when needed:

```bash
# Restrict to P-cores only (cores 0-3 on i7-1260P)
taskset -c 0-3 ./my_app

# Restrict to E-cores only (cores 4-11 on i7-1260P)
taskset -c 4-11 ./my_app
```

But the perf-book recommends against this for general-purpose software.

### Strategy 4: When affinity IS appropriate

Thread affinity should be used in specific scenarios:

```cpp
// 1. All cores are symmetric (no hybrid, single core type)
//    Affinity prevents migration overhead
cpu_set_t cpuset;
CPU_ZERO(&cpuset);
CPU_SET(target_core, &cpuset);
pthread_setaffinity_np(thread, sizeof(cpu_set_t), &cpuset);

// 2. NUMA-aware allocation on multi-socket servers
//    Pin thread to socket, allocate memory locally
#include <numa.h>
numa_run_on_node(socket_id);
void* data = numa_alloc_onnode(size, socket_id);

// 3. Latency-critical threads on isolated cores
//    Use isolcpus boot parameter + explicit affinity
// Boot with: isolcpus=14,15
// Then pin latency-critical thread to isolated core:
CPU_SET(14, &cpuset);
pthread_setaffinity_np(rt_thread, sizeof(cpu_set_t), &cpuset);
```

### Strategy 5: Account for frequency throttling in capacity planning

Frequency throttling means adding threads has diminishing returns even with perfect load balancing:

```bash
# Measure actual sustained frequency per thread count
for t in 1 2 4 8 16; do
  echo "=== Threads: $t ==="
  # Run workload and capture frequency simultaneously
  sudo turbostat --interval 1 -- ./my_app --threads=$t
done

# Compare: expected speedup (linear) vs. actual speedup
# vs. frequency-adjusted speedup = actual_freq(N) / actual_freq(1)
# The gap between frequency-adjusted and actual reveals software overhead
```

The perf-book's experiment: disabling Turbo Boost (forcing base frequency) doubled effective scaling for both Blender and Clang. This isolates software scaling issues from thermal effects. In production:
- Better cooling solutions reduce throttling
- Higher TDP processors sustain higher multi-core frequencies
- Frequency throttling accounts for a large portion of unrealized scaling on consumer hardware

## Expected Impact

- **Removing forced affinity** on hybrid systems: up to 35% improvement (perf-book: 864ms -> 567ms)
- **Dynamic scheduling** vs. static on hybrid: 10-40% improvement (perf-book: 567ms -> 517ms with optimal chunk size)
- **Optimal chunk size tuning**: 5-10% additional gain (perf-book: 16 chunks -> 128 chunks, 541ms -> 517ms)
- **NUMA-aware allocation** on multi-socket: 1.5-3x improvement for memory-bandwidth-bound workloads by eliminating remote memory access
- **Core isolation for latency-critical threads**: reduces jitter from 10s of microseconds to <1 microsecond by eliminating preemption

## Caveats

- **Hybrid scheduling is OS-version-dependent**: Intel Thread Director requires Linux 5.18+ and Windows 11. Older kernels treat all cores equally, causing suboptimal placement on hybrid processors. ARM EAS requires kernel 5.0+.
- **macOS has no affinity API**: you cannot pin threads to cores on macOS. The OS always controls thread placement. Design software to work well without affinity.
- **NUMA topology varies**: multi-socket servers have different NUMA distances. Use `numactl --hardware` to inspect the topology before setting affinity policies.
- **Dynamic scheduling is not free**: each chunk dispatch involves queue synchronization. The perf-book shows 1024 chunks was 8% slower than 128 chunks. The sweet spot depends on per-chunk work duration.
- **Turbo Boost frequency varies by instruction mix**: SIMD-heavy workloads (AVX2, AVX-512) operate at lower turbo frequencies than scalar workloads. A frequency chart from one workload cannot be applied to others.
- **Core isolation reduces total system capacity**: `isolcpus` removes cores from the OS scheduler's pool. Other processes get fewer cores. Only use for latency-critical real-time threads.
- **Work stealing has overhead**: stealing from another thread's deque requires synchronization. For very short tasks (< microsecond), the stealing overhead can exceed the task duration. Increase task granularity to amortize.
- **E-core/LITTLE-core performance is workload-dependent**: the 2x P-core vs. E-core ratio in the perf-book was for SIMD-heavy work. For memory-bound or integer-scalar work, the gap may be smaller (1.2-1.5x).
