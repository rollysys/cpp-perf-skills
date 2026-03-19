---
name: Cache Coherence Overhead Reduction
source: perf-book Ch.13 (Optimizing Multithreaded Applications)
layers: [microarchitecture, system]
platforms: [arm, x86]
keywords: [coherence, MESI, MESIF, MOESI, invalidation, cache line bounce, shared variable, ping-pong, true sharing, false sharing, perf c2c, HITM, RFO, contested accesses]
---

## Problem

Cache coherence protocols ensure all cores see a consistent view of memory, but this consistency has a direct performance cost. The perf-book Ch.13 explains the MESI protocol and its variants:

**MESI cache line states:**

| State | Description | Local read | Local write | Remote implications |
|-------|------------|------------|-------------|---------------------|
| **Modified (M)** | Only in this cache, dirty (differs from RAM) | Free | Free | Must supply data on snoop, writeback to RAM |
| **Exclusive (E)** | Only in this cache, clean (matches RAM) | Free | Free (silently transitions to M) | None unless snooped |
| **Shared (S)** | In multiple caches, clean | Free | **Expensive**: must invalidate all other copies (RFO) | Other caches must invalidate |
| **Invalid (I)** | Not present | **Cache miss** | **Cache miss** | None |

Intel uses MESIF (adds Forward state -- one cache designated as responder for shared lines). AMD uses MOESI (adds Owned state -- allows dirty data sharing without writeback). Both maintain the core MESI semantics.

The perf-book's key insight: "The only states that do not involve a costly cross-cache subsystem communication during CPU read/write operations are Modified (M) and Exclusive (E). The longer a cache line maintains M or E state, the lower the coherence cost."

**Two types of coherence problems:**

1. **True sharing**: multiple threads read and write the *same* variable. The cache line legitimately bounces between cores. Example from the perf-book:
```cpp
unsigned int sum;  // shared between all threads
// Thread A:                    // Thread B:
for (int i = 0; i < N; i++)    for (int i = 0; i < N; i++)
  sum += a[i];                    sum += b[i];
```
This is also a data race -- the perf-book notes that `sum` must be `std::atomic<unsigned int>` to be correct, but atomics serialize access and still cause cache line bouncing.

2. **False sharing**: threads write different variables that happen to reside on the same 64-byte (or 128-byte on Apple ARM) cache line. The bouncing is artificial:
```cpp
struct S {
  int sumA;  // sumA and sumB likely on the same cache line
  int sumB;
};
S s;
// Thread A writes s.sumA, Thread B writes s.sumB
// Cache line bounces even though they access independent data
```

The perf-book states: "In contrast to serialization and locking issues, which can only put a ceiling on the performance of the application, coherency issues can cause retrograde effects" as described by the Universal Scalability Law. Adding more threads actively makes performance *worse*.

## Detection

**TMA (Top-Down Microarchitecture Analysis):**
- High `Memory_Bound` -> `L3_Bound` -> `Contested_Accesses` = true/false sharing detected
- High `Memory_Bound` -> `L3_Bound` -> `Data_Sharing` = read-side sharing overhead

**x86-specific tools:**

```bash
# Stage 1: Run TMA to confirm sharing issues exist
# Using pmu-tools:
python toplev.py -l3 --no-desc -- ./my_app
# Look for: Memory_Bound.L3_Bound.Contested_Accesses

# Stage 2: perf c2c to find exact cache lines and source locations
perf c2c record -- ./my_app
perf c2c report --stdio
# Output shows:
# - Cache lines with highest HITM (Hit Modified) count
# - Source lines responsible for loads/stores to those lines
# - Which threads are involved in the contention

# RFO (Read For Ownership) count -- indicates write-sharing
perf stat -e l2_rqsts.all_rfo -- ./my_app
```

**Intel VTune two-stage process (recommended by perf-book):**
1. Run *Microarchitecture Exploration* analysis (implements TMA)
2. Check *Contested Accesses* metric
3. If high, run *Memory Access* analysis with "Analyze dynamic memory objects" enabled
4. This reveals specific data structures causing contention and their access latency

**ARM-specific:**

```bash
# Cache coherence events
perf stat -e bus_access,l2d_cache_refill -- ./my_app
# ARM SPE (Statistical Profiling Extension) where available provides
# per-access latency data similar to Intel PEBS
```

**Data race detection (related -- true sharing often involves races):**

```bash
# Clang ThreadSanitizer
clang++ -fsanitize=thread -g -O1 -o my_app my_app.cpp
./my_app
# Reports data races with stack traces

# Valgrind Helgrind
valgrind --tool=helgrind ./my_app
```

The perf-book explicitly recommends ThreadSanitizer and Helgrind for identifying data races in true sharing scenarios.

**Source-level indicators:**
- Global counters updated by multiple threads: `shared_counter++`
- `std::atomic<T>` with high store frequency from multiple threads
- Per-thread data in contiguous arrays: `results[thread_id] += value`
- `std::shared_ptr` reference counts shared across threads
- Lock variables themselves (every lock/unlock transitions the lock's cache line through MESI states)
- Producer-consumer buffer metadata (head/tail pointers) written by different threads

## Transformation

### Strategy 1: Thread Local Storage for true sharing elimination

The perf-book explicitly recommends TLS as the primary solution for true sharing:

```cpp
// BEFORE: true sharing with data race
unsigned int sum;
// Threads A and B both write to sum -- cache line bounces every iteration

// FIX 1: atomics (correct but still slow -- serializes + bounces)
std::atomic<unsigned int> sum;

// FIX 2 (recommended): thread_local + final merge
thread_local unsigned int local_sum = 0;
// Each thread accumulates locally, then:
#pragma omp atomic
global_sum += local_sum;  // single merge at the end

// FIX 3 (best for OpenMP): reduction clause
#pragma omp parallel for reduction(+:sum)
for (int i = 0; i < N; i++)
  sum += data[i];
// OpenMP creates thread-local copies automatically
```

`thread_local` (C++11) or `__thread` (GCC extension) place variables in per-thread storage, eliminating cross-cache sharing entirely. The cache line stays in M or E state (the cheapest states).

### Strategy 2: Alignment padding for false sharing elimination

The perf-book provides the exact transformation:

```cpp
// BEFORE: false sharing
struct S {
  int sumA;     // sumA and sumB on the same cache line
  int sumB;
};

// AFTER: padding to separate cache lines
constexpr int CacheLineAlign = 64;  // 128 on Apple M-series
struct S {
  int sumA;
  alignas(CacheLineAlign) int sumB;  // forces sumB to next cache line
};
```

For per-thread arrays, pad each element:

```cpp
struct alignas(64) PerThreadData {
  int accumulator;
  // implicit padding to 64 bytes
};
std::vector<PerThreadData> thread_data(num_threads);
```

**Critical note from the perf-book**: "Do not take the size of a cache line as a constant value. For example, in Apple processors such as M1, M2, and later, the L2 cache operates on 128B cache lines." Use:
```bash
# macOS
sysctl hw.cachelinesize

# Linux
cat /sys/devices/system/cpu/cpu0/cache/index0/coherency_line_size
```

Or use `std::hardware_destructive_interference_size` (C++17, but may not match actual cache line size on all targets).

### Strategy 3: Separate hot shared fields from cold/per-thread fields

For data structures with mixed access patterns, isolate fields by access frequency and sharing pattern:

```cpp
// BEFORE: producer and consumer fields on the same cache line
struct Queue {
  std::atomic<int> head;    // written by consumers
  std::atomic<int> tail;    // written by producer
  int stats;                // per-thread counter
};

// AFTER: each field on its own cache line
struct Queue {
  alignas(64) std::atomic<int> tail;   // producer cache line
  alignas(64) std::atomic<int> head;   // consumer cache line
  alignas(64) int stats;               // no sharing at all
};
```

This ensures producer and consumer only invalidate each other's lines when they *must* (reading the other's pointer to check for empty/full), not on every access.

### Strategy 4: Minimize time in Shared (S) state

Design data access so each cache line is predominantly owned by one core (M or E state):

```cpp
// BEFORE: all threads scan the same read-only config + write their own results
// Config data oscillates between S and I state as threads read/write nearby data
struct Work {
  Config config;           // read by all threads
  int results[MAX_THREADS]; // written by individual threads -- false sharing!
};

// AFTER: separate read-only shared data from per-thread mutable data
struct SharedConfig {
  alignas(64) Config config;  // stays in S state -- reads are free
};
struct alignas(64) ThreadResult {
  int value;  // stays in M state -- writes are free
};
SharedConfig shared;
std::vector<ThreadResult> results(num_threads);
```

The key: Shared (S) state is free for reads. Modified (M) and Exclusive (E) states are free for writes. Problems occur when a line repeatedly transitions between S and M (true/false sharing), incurring invalidation costs on every transition.

### Strategy 5: Use relaxed memory ordering where possible

Stronger memory ordering increases coherence traffic, especially on ARM:

```cpp
// Sequential consistency (default) -- maximum fence overhead
counter.fetch_add(1);  // memory_order_seq_cst implied

// Relaxed -- plain load/store on ARM, no fence
counter.fetch_add(1, std::memory_order_relaxed);

// Acquire/Release for producer-consumer (sufficient for most cases)
ready.store(true, std::memory_order_release);   // producer
while (!ready.load(std::memory_order_acquire));  // consumer
```

On ARM, `memory_order_seq_cst` emits DMB (Data Memory Barrier) instructions that stall the pipeline. On x86, the difference is smaller due to the strong memory model, but `seq_cst` stores still emit `MFENCE` or `XCHG`.

## Expected Impact

- **Eliminating true sharing** (TLS + merge): 2-10x improvement depending on contention level. The perf-book's `sum` example would achieve near-linear scaling after applying TLS.
- **Eliminating false sharing** (alignas padding): 2-8x improvement on 4-8 cores. HITM events drop to near zero.
- **Separating hot fields**: reduces cache-to-cache transfer latency from 20-100+ cycles per access to zero for the separated fields. Impact depends on access frequency.
- **Relaxed memory ordering** (ARM): saves 10-40 cycles per atomic operation by avoiding DMB barriers. On x86, saves 1-5 cycles (smaller due to strong memory model).
- **Overall**: the perf-book attributes retrograde scaling (performance *worse* with more threads) to coherence issues. Fixing these converts negative scaling into positive scaling.

## Caveats

- **True sharing cannot always be eliminated**: lock-free queues, work-stealing deques, and concurrent hash maps fundamentally require shared mutable state. Minimize contention frequency rather than trying to eliminate sharing entirely.
- **Padding wastes memory**: `alignas(64)` on a 4-byte variable wastes 60 bytes. For arrays of millions of elements, use TLS accumulation instead.
- **Cache line size varies**: x86 = 64 bytes universally. ARM varies: most ARM64 = 64 bytes, Apple M-series L2 = 128 bytes. The perf-book explicitly warns against hardcoding 64.
- **perf c2c requires PEBS** (Intel Precise Event-Based Sampling): not available on all x86 processors. On ARM, use SPE (Statistical Profiling Extension) where available. Not all ARM cores support SPE.
- **Memory ordering bugs are silent**: using `memory_order_relaxed` incorrectly causes data races that manifest rarely and non-deterministically. Always validate with ThreadSanitizer. When in doubt, use `seq_cst`.
- **False sharing in managed languages**: the perf-book notes that false sharing also occurs in Java and C#, where the programmer has less control over memory layout. JVM flags like `-XX:ContendedPaddingWidth` can help in Java.
- **Atomic `shared_ptr`** (C++20): `std::atomic<std::shared_ptr<T>>` uses internal locking on most implementations, adding unexpected serialization points. For high-frequency reads, consider raw pointer + epoch-based reclamation.
