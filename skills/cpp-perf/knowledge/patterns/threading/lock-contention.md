---
name: Lock Contention Reduction
source: perf-book Ch.13 (Optimizing Multithreaded Applications)
layers: [algorithm, system]
platforms: [arm, x86]
keywords: [mutex, contention, lock-free, atomic, spinlock, granularity, partitioning, sharding, futex, wait time, spin time, GIL, synchronization]
---

## Problem

Lock contention occurs when multiple threads compete for the same synchronization primitive, serializing execution and destroying parallel scaling. The perf-book Ch.13 identifies two distinct categories of wait:

1. **Sync Wait Time**: threads blocked on a contested lock, yielded by the OS scheduler. The perf-book states: "A large amount of Sync Wait Time likely indicates that the application has highly contended synchronization objects."
2. **Spin Time**: CPU-busy wait time where the thread polls a lock instead of yielding. Kernel synchronization primitives intentionally spin for a short period before blocking, but excessive spin time represents wasted CPU cycles that could be doing useful work.

The chapter's CPython case study is the extreme example: the Global Interpreter Lock (GIL) serializes all threads via `pthread_cond_timedwait` with a 5ms timeout. Two threads alternate execution -- never running simultaneously -- yielding zero speedup. VTune's Bottom-up view traced the entire contention path: `take_gil` -> `___pthread_cond_timedwait64`. The lock's 5ms yield interval caused thread A to work for 5ms, then thread B for 5ms, in strict alternation.

The Zstd case study shows a subtler form: worker threads stall for 20-40ms between jobs because the main thread holds the only path to posting new work. Worker threads finish compression, but the main thread is blocked waiting to flush data from an earlier (still in-progress) job. The shared memory pool adds further contention -- input/output buffers cannot be allocated until earlier jobs release theirs, and fragmentation in the single contiguous memory pool compounds the problem.

Even lock variables themselves cause cache coherence overhead: every `lock()`/`unlock()` transition walks through MESI states, bouncing the lock's cache line between cores.

## Detection

**Source-level indicators:**
- Global mutex protecting a large critical section (coarse-grained locking)
- `std::mutex`, `pthread_mutex_t`, or OS-level locks in hot paths
- Single producer thread that gates work distribution to all consumers (the Zstd pattern)
- Condition variables with long waits: `pthread_cond_wait`, `pthread_cond_timedwait`
- Shared resource pools (memory pools, buffer pools, connection pools) with single-lock access

**Profile-level detection:**

```bash
# Linux perf: lock contention analysis
perf lock record -- ./my_app
perf lock report
# Shows: lock name, number of contentions, total wait time, max wait time

# Context switches from contention
perf stat -e context-switches,cpu-migrations -- ./my_app
# High context-switches relative to useful work = lock contention

# eBPF-based contention tracking (no recompilation required)
# GAPP profiler: https://github.com/RN-dev-repo/GAPP/
# Traces futex syscalls, ranks serialization bottlenecks by criticality
# Collects stack traces of both blocked threads and blocking threads

# Intel VTune Threading Analysis
vtune -collect threading -r results -- ./my_app
# Reports: Wait Time (Sync + Preemption), Spin Time, lock call stacks
# Bottom-up view shows which locks cause the most Sync Wait Time
```

**Characteristic symptoms:**
- Thread timeline shows alternating execution (threads take turns instead of running in parallel)
- High Sync Wait Time in VTune threading analysis
- `perf lock report` shows specific locks with high contention count and total wait
- Adding threads provides no speedup or makes performance worse (retrograde per USL)
- High context-switch rate without corresponding throughput increase

## Transformation

### Strategy 1: Reduce critical section scope

Move computation out of the lock. Only protect the minimum shared state mutation:

```cpp
// BEFORE: entire operation under lock
std::mutex mtx;
void process(int id) {
  std::lock_guard lock(mtx);
  auto result = expensive_compute(id);  // serialized unnecessarily
  shared_data.update(result);
  log_operation(id, result);            // serialized unnecessarily
}

// AFTER: only protect the shared mutation
void process(int id) {
  auto result = expensive_compute(id);   // runs in parallel
  {
    std::lock_guard lock(mtx);
    shared_data.update(result);          // minimal critical section
  }
  log_operation(id, result);             // runs in parallel
}
```

### Strategy 2: Partition shared resources (sharding)

Replace a single global lock with N independent locks, each protecting a partition of the data:

```cpp
// BEFORE: single lock for entire hash map
std::mutex map_lock;
std::unordered_map<Key, Value> global_map;

void insert(Key k, Value v) {
  std::lock_guard lock(map_lock);  // all threads serialize here
  global_map[k] = v;
}

// AFTER: sharded map with per-shard locks
static constexpr int NUM_SHARDS = 64;
struct Shard {
  alignas(64) std::mutex lock;   // each shard on its own cache line
  std::unordered_map<Key, Value> map;
};
std::array<Shard, NUM_SHARDS> shards;

void insert(Key k, Value v) {
  auto& shard = shards[std::hash<Key>{}(k) % NUM_SHARDS];
  std::lock_guard lock(shard.lock);  // contention reduced by ~NUM_SHARDS
  shard.map[k] = v;
}
```

The number of shards should be >= the number of threads. Use `alignas(64)` on each shard to prevent false sharing on the lock variables themselves.

### Strategy 3: Decouple producer-consumer with buffered queues

The Zstd case study shows the main thread becomes a bottleneck because it both prepares work and flushes results through a single path. Decouple these roles:

```cpp
// BEFORE (Zstd pattern): main thread prepares jobs AND flushes output
// Workers stall 20-40ms waiting for the main thread to post new work
// because the main thread is blocked waiting to flush earlier job data

// AFTER: separate preparation and flushing, use deeper job queues
// - Pre-allocate enough input buffers so job preparation never stalls
//   on buffer availability
// - Use a dedicated flush thread instead of blocking the main thread
// - Allow the main thread to fill the job queue ahead of workers

// Key insight from perf-book: Zstd limits buffer pool capacity to
// reduce memory consumption, but this creates artificial serialization.
// The trade-off between memory usage and parallelism must be tuned
// for the target workload.
```

### Strategy 4: Replace blocking locks with try-lock + fallback

When contention is intermittent, avoid blocking entirely:

```cpp
// BEFORE: always block on contention
std::mutex mtx;
void process(Item item) {
  std::lock_guard lock(mtx);
  shared_queue.push(item);
}

// AFTER: try-lock with thread-local batching fallback
thread_local std::vector<Item> local_batch;

void process(Item item) {
  local_batch.push_back(item);
  if (local_batch.size() >= BATCH_SIZE) {
    std::lock_guard lock(mtx);
    for (auto& i : local_batch)
      shared_queue.push(i);
    local_batch.clear();
  }
}
```

Batching amortizes lock acquisition cost over many items and reduces contention frequency by a factor of `BATCH_SIZE`.

### Strategy 5: Use Coz profiler to quantify lock impact

The perf-book introduces Coz as a causal profiler that predicts the overall impact of optimizing specific code regions. For lock contention, Coz can answer: "if we made the critical section 20% faster, how much would overall throughput improve?"

```bash
# Build with debug info, link with Coz
coz run --- ./my_app
# Output example: "improving line 540 by 20% -> 17% overall improvement"
# Once improvement reaches ~45%, impact levels off (Amdahl ceiling)
```

This avoids wasted effort: if Coz shows that speeding up a contended region yields negligible overall improvement, the serial fraction may be elsewhere.

## Expected Impact

- **Reducing critical section scope**: proportional to the ratio of removed computation. If 80% of the locked region is moved out, contention drops by ~80%.
- **Sharding**: reduces contention probability by a factor of `NUM_SHARDS`. With 64 shards and 16 threads, expected contention drops by ~4x.
- **Decoupled producer-consumer**: the perf-book's Zstd timeline shows 20-40ms worker stall gaps between jobs. Eliminating these could improve throughput by 15-30% for that workload.
- **Batching**: reduces lock acquisitions by `BATCH_SIZE`. With batch=64, lock overhead drops by 64x.
- **Overall**: the perf-book states that contention issues (along with coherence) cause the "retrograde" effects described by the Universal Scalability Law -- performance that degrades beyond a critical thread count. Fixing contention can convert retrograde scaling into positive scaling.

## Caveats

- **Lock-free is not always faster**: lock-free data structures avoid blocking but introduce their own overhead from CAS retry loops and memory ordering constraints. The perf-book explicitly states it does not cover lock-free structures as they are "well covered in other books." Use them only when profiling confirms lock contention is the bottleneck.
- **Sharding increases complexity**: N shards means N locks to manage. Iteration over all data requires acquiring all shard locks. Deadlock risk increases if operations span multiple shards.
- **Try-lock can cause starvation**: if a thread repeatedly fails `try_lock`, it may make no progress. Always provide a fallback path (batching, exponential backoff, or eventual blocking).
- **Spin time is not always bad**: kernel lock implementations intentionally spin briefly before blocking because context-switch overhead (1-10 microseconds) exceeds the cost of short spin waits. Only worry about spin time when it constitutes a large fraction of total CPU time.
- **GIL-like locks require architectural changes**: when a single global lock serializes all threads (CPython's GIL), no amount of lock tuning helps. The solution is architectural: GIL-immune libraries (NumPy), C extension modules, or alternative runtimes (Python 3.13 `--disable-gil`).
- **Profiling overhead**: GAPP and eBPF-based tools trace `futex` syscalls in the kernel. Their overhead is low but non-zero. VTune's Threading Analysis uses instrumentation that can perturb timing-sensitive lock behavior.
