---
name: Software Memory Prefetching
source: perf-book Ch.8, perf-ninja memory_bound/swmem_prefetch_1
layers: [microarchitecture, system]
platforms: [arm, x86]
keywords: [prefetch, __builtin_prefetch, cache miss, latency hiding, stride, linked list, pointer chasing]
---

## Problem

When memory access patterns are irregular (random indexing, hash table lookups, pointer chasing), the hardware prefetcher cannot predict the next address to fetch. If the load latency dominates the loop body, the CPU stalls waiting for data from DRAM (200-400 cycles). The Out-of-Order engine can overlap some latency with other work, but only if there is enough independent work within its reorder buffer window.

The perf-ninja `swmem_prefetch_1` lab demonstrates this: lookups into a 32M-entry hash map produce essentially random memory accesses. Each `hash_map->find(val)` misses in cache because the bucket address is unpredictable.

```cpp
// Random lookups into a large hash map -- every find() is a cache miss
int solution(const hash_map_t *hash_map, const std::vector<int> &lookups) {
  int result = 0;
  for (int val : lookups) {
    if (hash_map->find(val))       // cache miss: random bucket access
      result += getSumOfDigits(val);
  }
  return result;
}
```

## Detection

**Source-level indicators:**
- Loops with indirect array access: `arr[index[i]]` where `index` is not sequential
- Hash map / hash set lookups in a loop
- Pointer chasing: `node = node->next` in linked lists or tree traversals
- Large data structures (working set >> L2 cache) with non-sequential access

**Profile-level indicators:**
- High LLC (Last Level Cache) miss rate
- TMA: high `Memory_Bound > L3_Bound` or `DRAM_Bound`
- `perf stat`: high `LLC-load-misses` relative to `LLC-loads`
- Long stall cycles on load instructions (visible in `perf annotate`)

**Prefetching window assessment:**
The prefetching window is the interval between when the target address becomes known and when the data is consumed. If this window is shorter than memory latency (~200-400 cycles for DRAM), the load sits on the critical path.

## Transformation

### Strategy 1: Prefetch for the next iteration (software pipelining)

Compute the address for iteration N+1 and issue a prefetch while processing iteration N:

```cpp
// Before: no prefetching
for (int i = 0; i < N; ++i) {
  size_t idx = random_distribution(generator);
  int x = arr[idx];   // cache miss, stalls here
  doSomeExtensiveComputation(x);
}
```

```cpp
// After: software pipelining with prefetch
size_t idx = random_distribution(generator);
for (int i = 0; i < N; ++i) {
  int x = arr[idx];
  idx = random_distribution(generator);
  __builtin_prefetch(&arr[idx]);  // prefetch for next iteration
  doSomeExtensiveComputation(x);  // hides the prefetch latency
}
```

The key insight: `doSomeExtensiveComputation(x)` provides the prefetching window. By the time the next iteration starts, the data is already in cache.

### Strategy 2: Prefetch K iterations ahead (for short loop bodies)

When each iteration does little work, prefetch multiple iterations ahead to create a sufficient window:

```cpp
// Prefetch lookAhead iterations ahead for hash map lookups
template <int lookAhead = 8>
int solution(const hash_map_t *hash_map, const std::vector<int> &lookups) {
  int result = 0;
  int n = lookups.size();

  for (int i = 0; i < n; i++) {
    // Prefetch the hash bucket for a future iteration
    if (i + lookAhead < n) {
      int future_val = lookups[i + lookAhead];
      int future_bucket = future_val % hash_map->bucket_count();
      __builtin_prefetch(&hash_map->data()[future_bucket]);
    }

    if (hash_map->find(lookups[i]))
      result += getSumOfDigits(lookups[i]);
  }
  return result;
}
```

**Choosing `lookAhead`:** The prefetch distance should be approximately:
```
lookAhead = memory_latency_cycles / loop_body_cycles
```
For DRAM latency ~300 cycles and a loop body of ~40 cycles: `lookAhead = 300/40 ~ 8`.

### Strategy 3: Graph/tree traversal prefetching

For graph algorithms where future vertices are known from an edge list:

```cpp
template <int lookAhead = 8>
void Graph::update(const std::vector<Edge>& edges) {
  for (int i = 0; i + lookAhead < edges.size(); i++) {
    VertexID v = edges[i].from;
    VertexID u = edges[i].to;
    this->out_neighbors[u].push_back(v);
    this->in_neighbors[v].push_back(u);

    // prefetch for future iteration
    VertexID v_next = edges[i + lookAhead].from;
    VertexID u_next = edges[i + lookAhead].to;
    __builtin_prefetch(this->out_neighbors.data() + v_next);
    __builtin_prefetch(this->in_neighbors.data()  + u_next);
  }
  // handle the remaining lookAhead elements without prefetch
}
```

### `__builtin_prefetch` API

```cpp
__builtin_prefetch(const void *addr, int rw = 0, int locality = 3);
```
- `addr`: address to prefetch
- `rw`: 0 = prefetch for read (default), 1 = prefetch for write
- `locality`: temporal locality hint (0 = no reuse, 3 = high reuse / keep in all caches)

**Platform intrinsics:**
- x86: `_mm_prefetch(addr, _MM_HINT_T0)` -- generates `PREFETCHT0`
- ARM: `__pld(addr)` -- generates `PLD` instruction

## Expected Impact

- **Typical speedup:** 1.5-3x for loops dominated by random cache misses when the prefetching window is sufficient.
- **Best case:** when `doSomeExtensiveComputation()` is long enough to fully hide DRAM latency (~300 cycles), the cache miss penalty approaches zero.
- **perf-ninja swmem_prefetch_1:** prefetching hash map lookups yields significant speedup on large (32M entry) maps.
- **Verification:** confirm LLC miss count drops with `perf stat -e LLC-load-misses`.

## Caveats

- **Not portable:** a prefetch strategy tuned for one microarchitecture may hurt performance on another. The CPU is allowed to ignore prefetch hints entirely.
- **Useless for sequential access:** hardware prefetchers handle sequential and simple strided patterns well. Adding software prefetch on top adds instruction overhead with no benefit.
- **Pointer chasing is fundamentally hard:** in a linked list traversal (`node = node->next`), the next address is only known after the current load completes. The prefetching window is zero -- software prefetch cannot help. Restructure the data (e.g., flatten into an array) instead.
- **Prefetch too early = cache pollution:** data brought in too soon gets evicted before use, wasting bandwidth and evicting other useful data.
- **Prefetch too late = no benefit:** if the prefetch does not arrive before the demand load, it provides no speedup.
- **Code size overhead:** each `__builtin_prefetch` is an extra instruction that consumes frontend bandwidth and decode slots.
- **Conditional/irregular loops:** if the loop has `continue` or `break` statements, the prefetched address may not actually be accessed, wasting bandwidth. Verify prefetch accuracy by instrumentation.
