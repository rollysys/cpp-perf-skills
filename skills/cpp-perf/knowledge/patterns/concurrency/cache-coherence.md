---
name: Cache Coherence and Sharing Optimization
source: perf-book Ch.13
layers: [system, microarchitecture]
platforms: [arm, x86]
keywords: [cache coherence, MESI, MOESI, true sharing, false sharing, read-mostly, RCU, per-thread, thread local, cacheline bouncing]
---

## Problem

Cache coherence protocols (MESI, MESIF on Intel, MOESI on AMD) ensure that all cores see a consistent view of memory. This consistency comes at a cost: when one core modifies a cache line, all other cores holding that line must invalidate or update their copies. This cross-core communication (called "snooping") consumes interconnect bandwidth and adds latency to memory accesses.

The perf-book Ch.13 identifies two types of coherence problems:

1. **True sharing**: multiple threads read AND write the *same* variable. The cache line legitimately bounces between cores because the data is genuinely shared. This is the harder problem -- you cannot simply pad it away.
2. **False sharing**: multiple threads write to *different* variables that happen to occupy the same cache line. The bouncing is artificial and can be eliminated with alignment/padding. (Covered in detail in the `memory/false-sharing.md` pattern.)

Both problems cause retrograde speedup as described by the Universal Scalability Law (USL) -- performance that degrades as you add more threads, not just fails to scale. The perf-book states: "In contrast to serialization and locking issues, which can only put a ceiling on the performance of the application, coherency issues can cause retrograde effects."

### MESI Protocol States

Every cache line is tagged with one of four states:

| State | Meaning | Read cost | Write cost |
|-------|---------|-----------|------------|
| **Modified (M)** | Only in this cache, dirty | Free | Free |
| **Exclusive (E)** | Only in this cache, clean | Free | Free (transition to M) |
| **Shared (S)** | In multiple caches, clean | Free | Expensive (must invalidate all other copies) |
| **Invalid (I)** | Not in cache | Cache miss | Cache miss |

Key insight from the perf-book: "The only states that do not involve a costly cross-cache subsystem communication during CPU read/write operations are Modified (M) and Exclusive (E). The longer a cache line maintains M or E state, the lower the coherence cost."

Intel extends MESI with a **Forward (F)** state (MESIF) to designate one cache as the responder for shared lines. AMD adds an **Owned (O)** state (MOESI) allowing dirty sharing without writeback to memory.

## Detection

**Source-level indicators (true sharing):**
- Global counters or accumulators updated by multiple threads: `shared_counter++`
- Shared data structures with concurrent readers and writers (e.g., concurrent queues, shared hash maps)
- Atomic variables under heavy contention: `std::atomic<T>` with high store frequency from multiple threads
- Producer-consumer patterns where the shared buffer metadata (head/tail pointers) is frequently updated

**Source-level indicators (cacheline bouncing in general):**
- Any frequently written variable that is visible to multiple cores
- Lock variables themselves (mutex internal state transitions through MESI on every lock/unlock)
- Reference counts in shared_ptr when objects are shared across threads

**Profile-level indicators:**

```bash
# x86: detect cache line contention with perf c2c
perf c2c record -- ./my_app
perf c2c report
# Look for HITM (Hit Modified) events -- these indicate cache-to-cache transfers
# on lines where one core's write invalidated another core's copy

# TMA metrics
# High Memory_Bound > L3_Bound > Contested_Accesses = true/false sharing
# High Memory_Bound > L3_Bound > Data_Sharing = read sharing overhead

# x86: RFO (Read For Ownership) counts
perf stat -e l2_rqsts.all_rfo -- ./my_app
# High RFO count means cores are frequently taking ownership of shared lines

# ARM: cache coherence events
perf stat -e bus_access,l2d_cache_refill -- ./my_app
```

**Tools for data race detection** (related, since true sharing often involves races):
- Clang ThreadSanitizer: `clang++ -fsanitize=thread -g -O1`
- Valgrind Helgrind: `valgrind --tool=helgrind ./my_app`

**Characteristic symptom:** performance degrades as threads are added, even though the workload is embarrassingly parallel. HITM events in `perf c2c` are concentrated on a small number of cache lines.

## Transformation

### Strategy 1: Thread-local storage to eliminate true sharing

The most effective solution for true sharing is to give each thread its own copy and merge results at the end. The perf-book explicitly recommends this:

```cpp
// BEFORE: true sharing -- all threads hammer the same variable
unsigned int sum;  // data race + cache line bouncing
#pragma omp parallel for
for (int i = 0; i < N; i++)
  sum += data[i];  // every thread writes to the same cache line

// AFTER: thread_local accumulation + final reduction
thread_local unsigned int local_sum = 0;

#pragma omp parallel
{
  local_sum = 0;
  #pragma omp for
  for (int i = 0; i < N; i++)
    local_sum += data[i];  // each thread writes to its own TLS copy

  #pragma omp atomic
  sum += local_sum;  // single merge at the end
}
```

Or with OpenMP reduction clause (preferred -- compiler handles the details):

```cpp
unsigned int sum = 0;
#pragma omp parallel for reduction(+:sum)
for (int i = 0; i < N; i++)
  sum += data[i];  // OpenMP creates thread-local copies automatically
```

The `thread_local` keyword (C++11) or `__thread` (GCC extension) place variables in Thread Local Storage, eliminating cross-thread cache line sharing entirely.

### Strategy 2: Per-thread data structures with final merge

For complex data structures (not just scalars), maintain per-thread instances:

```cpp
// BEFORE: shared histogram with atomic increments
std::array<std::atomic<int>, 256> global_histogram{};

void process(const uint8_t* data, size_t len) {
  #pragma omp parallel for
  for (size_t i = 0; i < len; i++)
    global_histogram[data[i]]++;  // massive contention on hot buckets
}

// AFTER: per-thread histograms, merge at end
void process(const uint8_t* data, size_t len) {
  int num_threads = omp_get_max_threads();
  // Each thread gets its own 256-entry histogram, cache-line aligned
  std::vector<std::array<int, 256>> local_hists(num_threads);

  #pragma omp parallel
  {
    auto& my_hist = local_hists[omp_get_thread_num()];
    my_hist.fill(0);

    #pragma omp for
    for (size_t i = 0; i < len; i++)
      my_hist[data[i]]++;  // purely local writes, no coherence traffic
  }

  // Serial merge -- typically negligible cost
  std::array<int, 256> result{};
  for (auto& h : local_hists)
    for (int i = 0; i < 256; i++)
      result[i] += h[i];
}
```

This pattern applies to any commutative, associative reduction: sums, min/max, histograms, sets, counters.

### Strategy 3: Read-mostly optimization (RCU-like patterns)

When data is read frequently but updated rarely, use Read-Copy-Update (RCU) semantics to eliminate write contention on the read path:

```cpp
// BEFORE: reader-writer lock -- even readers contend on the lock variable
std::shared_mutex rw_lock;
std::shared_ptr<Config> config;

Config read_config() {
  std::shared_lock lock(rw_lock);  // lock variable still bounces between cores
  return *config;
}

// AFTER: RCU-like pattern with atomic pointer swap
// Readers are lock-free; only the writer pays synchronization cost
std::atomic<std::shared_ptr<Config>> config;

Config read_config() {
  return *config.load(std::memory_order_acquire);  // no lock, no bouncing
}

void update_config(Config new_cfg) {
  auto new_ptr = std::make_shared<Config>(std::move(new_cfg));
  config.store(new_ptr, std::memory_order_release);  // single atomic store
  // Old config is freed when the last reader releases its shared_ptr
}
```

For even lower overhead, use a double-buffering or epoch-based reclamation scheme:

```cpp
// Epoch-based RCU: readers announce their epoch, writer waits for all readers
// to leave the old epoch before reclaiming memory
// Libraries: libcds, folly::RCU, userspace-rcu (liburcu)
```

The key insight: `std::shared_mutex` itself has a cache line that bounces between reader cores on every `shared_lock`/`shared_unlock` call. For truly read-heavy workloads (>95% reads), even reader-writer locks cause measurable contention. RCU eliminates read-side synchronization entirely.

### Strategy 4: Reduce lock scope and granularity

When true sharing through locks is unavoidable, minimize the time the lock is held and the data it protects:

```cpp
// BEFORE: coarse-grained lock -- all operations serialized
std::mutex global_lock;
void process(int id, Data& data) {
  std::lock_guard lock(global_lock);
  // entire operation is serialized
  data.compute(id);
  data.update(id);
  data.log(id);
}

// AFTER: fine-grained locking -- only protect shared mutation
void process(int id, Data& data) {
  auto local_result = compute_locally(id);  // no lock needed

  {
    std::lock_guard lock(data.update_mutex);
    data.apply(local_result);  // minimal critical section
  }

  log_locally(local_result);  // no lock needed
}
```

### Strategy 5: Atomic operations with appropriate memory ordering

When you must share data, use the weakest sufficient memory ordering to minimize cache coherence traffic:

```cpp
// BEFORE: sequential consistency (default) -- maximum fence overhead
std::atomic<int> counter;
counter.fetch_add(1);  // implied memory_order_seq_cst

// AFTER: relaxed ordering when total order is not needed
// (e.g., a statistics counter where approximate count is acceptable)
counter.fetch_add(1, std::memory_order_relaxed);

// For producer-consumer flags:
std::atomic<bool> ready;
// Producer:
data = 42;
ready.store(true, std::memory_order_release);
// Consumer:
while (!ready.load(std::memory_order_acquire)) {}
use(data);
```

On ARM, this matters significantly: `memory_order_seq_cst` emits `DMB` (Data Memory Barrier) instructions that stall the pipeline. `memory_order_relaxed` compiles to plain loads/stores on ARM. On x86, the difference is smaller due to the strong memory model, but `seq_cst` stores still emit `MFENCE` or `XCHG`.

### Strategy 6: Detect cacheline bouncing with perf c2c

The perf-book recommends a two-stage process:

```bash
# Stage 1: Run TMA to detect presence of sharing issues
perf stat -e cpu/event=0xd1,umask=0x02/,cpu/event=0xd1,umask=0x04/ -- ./my_app
# Or use pmu-tools for TMA:
# python toplev.py -l3 --no-desc -- ./my_app
# Look for: Memory_Bound.L3_Bound.Contested_Accesses

# Stage 2: If sharing detected, use perf c2c to find the exact cache lines
perf c2c record -- ./my_app
perf c2c report --stdio
# Output shows:
# - Cache lines with highest HITM count
# - Source lines responsible for loads/stores to those lines
# - Which threads are involved in the contention
```

On Intel VTune:
1. Run *Microarchitecture Exploration* analysis (implements TMA)
2. Check *Contested Accesses* metric
3. If high, run *Memory Access* analysis with "Analyze dynamic memory objects" enabled
4. This reveals which data structures cause contention and the latency of those accesses

### Strategy 7: Padding and alignment for mixed true+false sharing

Sometimes a data structure has both legitimately shared fields and per-thread fields. Separate them:

```cpp
// BEFORE: shared and per-thread data on the same cache line
struct WorkQueue {
  std::atomic<int> head;    // written by consumers (true sharing, unavoidable)
  std::atomic<int> tail;    // written by producer
  int local_stats;          // per-thread counter -- false sharing with head/tail
  char padding_[48];        // insufficient
};

// AFTER: separate hot shared fields from cold/per-thread fields
struct WorkQueue {
  // --- Cache line 1: producer side ---
  alignas(64) std::atomic<int> tail;

  // --- Cache line 2: consumer side ---
  alignas(64) std::atomic<int> head;

  // --- Cache line 3: per-thread stats (no sharing) ---
  alignas(64) int local_stats;
};
```

This ensures `head` and `tail` are on separate cache lines, so producer and consumer only invalidate each other's lines when they *must*, not on every access. The `local_stats` field never causes coherence traffic.

## Expected Impact

- **Eliminating true sharing** (thread-local + reduction): can yield 2-10x improvement depending on contention level. The perf-book's true sharing example with `sum` would see near-linear scaling after the fix.
- **RCU for read-mostly data**: eliminates read-side synchronization entirely. For workloads with >95% reads, this can improve throughput by 5-50x compared to `shared_mutex`.
- **Appropriate memory ordering**: on ARM, switching from `seq_cst` to `release/acquire` avoids `DMB` barriers, saving 10-40 cycles per atomic operation. On x86, savings are smaller (1-5 cycles) but accumulate in tight loops.
- **perf c2c analysis**: identifies the exact cache lines and source locations causing contention. This is the fastest path to finding coherence bottlenecks.
- **Cache line bouncing reduction**: keeping lines in M or E state (no sharing) avoids cross-cache transfers that cost 20-100+ cycles per access depending on interconnect topology (ring bus vs. mesh).

## Caveats

- **True sharing cannot always be eliminated**: some algorithms fundamentally require shared mutable state (e.g., work-stealing deques, concurrent hash maps). In these cases, minimize contention frequency rather than trying to eliminate sharing. Lock-free data structures help but are extremely difficult to implement correctly.
- **thread_local has initialization cost**: each thread gets its own copy, which must be initialized. For large per-thread structures, the memory footprint scales linearly with thread count.
- **RCU memory reclamation is tricky**: naive RCU leaks memory if readers hold references for a long time. Use epoch-based reclamation or hazard pointers. Libraries like `libcds` or `folly::RCU` handle this correctly.
- **Memory ordering bugs are silent**: using `memory_order_relaxed` incorrectly leads to data races that manifest rarely and non-deterministically. Always validate with ThreadSanitizer (`-fsanitize=thread`). When in doubt, use `seq_cst`.
- **perf c2c requires hardware support**: it uses PEBS (Precise Event-Based Sampling) on Intel processors. Not available on all platforms. On ARM, use Arm SPE (Statistical Profiling Extension) where available.
- **Cache line size varies**: x86 uses 64 bytes universally, but ARM varies. Apple M-series uses 128-byte L2 lines. Verify with `sysctl hw.cachelinesize` (macOS) or `/sys/devices/system/cpu/cpu0/cache/index0/coherency_line_size` (Linux). The perf-book explicitly notes: "Do not take the size of a cache line as a constant value."
- **Atomic `shared_ptr` has overhead**: `std::atomic<std::shared_ptr<T>>` (C++20) uses internal locking on most implementations. For high-frequency reads, consider raw pointer + epoch-based reclamation instead.
- **False sharing is the easier problem**: if `perf c2c` shows HITM on lines where threads access *different* variables, that is false sharing -- fix with `alignas(64)` padding. See the `memory/false-sharing.md` pattern for details. Focus on true sharing only after false sharing is eliminated.
