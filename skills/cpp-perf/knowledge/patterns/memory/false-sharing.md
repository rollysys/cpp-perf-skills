---
name: False Sharing Elimination
source: perf-ninja memory_bound/false_sharing_1, perf-book Ch.8
layers: [system]
platforms: [arm, x86]
keywords: [false sharing, cache line, thread, alignas, padding, multithreaded, contention, atomic]
---

## Problem

False sharing occurs when multiple threads write to different variables that reside on the same cache line (64 bytes on x86 and most ARM). Even though the threads access logically independent data, the cache coherence protocol (MESI/MOESI) forces the cache line to bounce between cores on every write. Each bounce costs 20-100+ cycles depending on the interconnect topology.

The perf-ninja `false_sharing_1` lab demonstrates this directly: multiple OpenMP threads write to adjacent `std::atomic<uint32_t>` accumulators packed into a `std::vector`. Since each `Accumulator` is only 4 bytes, up to 16 accumulators fit on one 64-byte cache line, causing severe contention.

```cpp
// False sharing: accumulators for different threads are adjacent in memory
struct Accumulator {
  std::atomic<uint32_t> value = 0;  // 4 bytes
};
std::vector<Accumulator> accumulators(thread_count);  // packed contiguously

#pragma omp parallel num_threads(thread_count)
{
  int target_index = omp_get_thread_num();
  auto &target = accumulators[target_index];

  #pragma omp for
  for (int i = 0; i < data.size(); i++) {
    auto item = data[i];
    item += 1000;
    item ^= 0xADEDAE;
    item |= (item >> 24);
    target.value += item % 13;  // writes to adjacent cache lines = false sharing
  }
}
```

With 4 threads, all 4 accumulators fit in a single 64-byte cache line. Every write by any thread invalidates the line for all other threads.

## Detection

**Source-level indicators:**
- Per-thread counters/accumulators stored in a contiguous array
- Shared arrays where each thread writes to its own index (e.g., `results[thread_id]`)
- Structs/arrays smaller than 64 bytes shared across threads with frequent writes

**Profile-level indicators:**
- `perf c2c record / perf c2c report`: identifies cache lines with high cross-core contention. Look for "HITM" (Hit Modified) events.
- TMA: high `Memory_Bound > L3_Bound` with high `Contested_Accesses` or `Data_Sharing`
- Poor multi-threaded scaling: adding threads provides little or negative speedup despite ample parallelism
- `perf stat -e l2_rqsts.all_rfo`: high RFO (Read For Ownership) count indicates cache line bouncing

**Characteristic symptom:** a parallel loop that is *slower* with more threads, or scales far worse than expected.

## Transformation

### Strategy 1: Pad each accumulator to a full cache line

Use `alignas` to force each thread's data onto its own cache line:

```cpp
// After: each accumulator occupies its own 64-byte cache line
struct alignas(64) Accumulator {
  std::atomic<uint32_t> value = 0;
  // implicit padding to 64 bytes from alignas
};
std::vector<Accumulator> accumulators(thread_count);
```

Now each `Accumulator` is 64 bytes (one full cache line). No two threads share a cache line.

### Strategy 2: Thread-local accumulation with final reduction

Avoid shared memory entirely during the hot loop. Each thread accumulates into a local variable, then merges results:

```cpp
std::size_t solution(const std::vector<uint32_t> &data, int thread_count) {
  std::vector<std::size_t> partial_results(thread_count, 0);

  #pragma omp parallel num_threads(thread_count)
  {
    std::size_t local_sum = 0;  // thread-local, lives in a register

    #pragma omp for
    for (int i = 0; i < data.size(); i++) {
      auto item = data[i];
      item += 1000;
      item ^= 0xADEDAE;
      item |= (item >> 24);
      local_sum += item % 13;
    }

    partial_results[omp_get_thread_num()] = local_sum;  // single write at end
  }

  std::size_t result = 0;
  for (auto v : partial_results) result += v;
  return result;
}
```

This is the best approach: the compiler keeps `local_sum` in a register during the entire loop. Zero cache coherence traffic.

### Strategy 3: C++17 `std::hardware_destructive_interference_size`

Use the standard constant for portable padding:

```cpp
#include <new>  // for std::hardware_destructive_interference_size

struct alignas(std::hardware_destructive_interference_size) Accumulator {
  std::atomic<uint32_t> value = 0;
};
```

Note: `std::hardware_destructive_interference_size` is `constexpr` but may not match the actual cache line size on all targets. On most x86 and ARM64, it is 64.

## Expected Impact

- **Speedup:** perf-ninja expects at least 60% improvement. In practice, eliminating false sharing on 4-8 cores can yield 2-8x speedup depending on how much time is spent in the contended loop.
- **Scaling:** with false sharing eliminated, speedup should scale roughly linearly with thread count (assuming sufficient memory bandwidth).
- **Cache coherence traffic:** HITM events should drop to near zero after the fix.
- **Single-threaded overhead:** the padding approach wastes `(64 - sizeof(value))` bytes per accumulator. For a small number of threads this is negligible. Thread-local accumulation has zero overhead.

## Caveats

- **Only matters for writes:** false sharing on read-only data is not a problem (MESI Shared state allows multiple readers). The issue is write-write or write-read contention.
- **Padding wastes memory:** `alignas(64)` on a 4-byte struct wastes 60 bytes per element. For millions of elements this is unacceptable -- use thread-local accumulation instead.
- **`std::vector` alignment:** `std::vector<alignas(64) T>` requires an aligned allocator. Default `std::allocator` may not respect over-alignment on older compilers. Use `std::aligned_alloc` or a custom allocator if needed.
- **ARM cache line size:** most ARM64 cores use 64-byte lines, but some (e.g., Apple M-series) use 128-byte lines for certain cache levels. Verify with `/sys/devices/system/cpu/cpu0/cache/index0/coherency_line_size` (Linux) or `sysctl hw.cachelinesize` (macOS).
- **Don't confuse with true sharing:** if threads genuinely need to read each other's writes frequently (e.g., work-stealing queues), padding won't help -- you need a different algorithm.
- **Atomic operations have their own overhead:** even without false sharing, `std::atomic` has fence/barrier costs. Thread-local accumulation avoids both false sharing AND atomic overhead.
