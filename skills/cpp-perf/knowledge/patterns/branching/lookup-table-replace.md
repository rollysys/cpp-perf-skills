---
name: Replace Branches with Lookup Tables
source: perf-ninja bad_speculation/lookup_tables_1, perf-book Ch.10
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [lookup table, switch, branch, LUT, jump table, indirect, precompute, dispatch]
---

## Problem

When a function maps an input value to an output through a chain of `if/else` or `switch/case` comparisons, each comparison generates a branch. If the input values are uniformly distributed across the range, the branch predictor cannot learn a stable pattern and mispredicts frequently. For N buckets with uniform distribution, the best a predictor can do is ~1/N accuracy per branch, leading to a cascade of mispredictions.

Common trigger: a mapping function with multiple thresholds, called in a hot loop with diverse input values.

**perf-ninja example -- histogram bucketing:**

Values in `[0, 150]` are mapped into 8 buckets via a 7-deep `if/else` chain. With uniform distribution, each comparison has roughly equal probability of going either way.

```cpp
// Original: if/else chain -- up to 7 branches per call
static std::size_t mapToBucket(std::size_t v) {
    if      (v < 13)  return 0;   // bucket size: 13
    else if (v < 29)  return 1;   // bucket size: 16
    else if (v < 41)  return 2;   // bucket size: 12
    else if (v < 53)  return 3;   // bucket size: 12
    else if (v < 71)  return 4;   // bucket size: 18
    else if (v < 83)  return 5;   // bucket size: 12
    else if (v < 100) return 6;   // bucket size: 17
    return DEFAULT_BUCKET;        // bucket 7 (values >= 100)
}
```

This function is called 1M times (`NUM_VALUES = 1024 * 1024`), with random values in `[0, 150]`. The default bucket catches ~33% of values.

## Detection

**Source-level indicators:**
- `if/else` chains or `switch/case` with 4+ branches mapping values to categories
- The mapping is from a bounded integer range to discrete outputs
- Called in a hot loop with diverse (non-sorted, non-clustered) input data
- Each branch depends on the input value, not on loop-invariant conditions

**Profile-level indicators:**
- TMA: high `Bad_Speculation > Branch_Mispredict` on the mapping function
- `perf stat`: high `branch-misses` count attributed to the if/else chain
- The function shows up as a hotspot despite doing trivial work (just comparisons and returns)

**Disassembly clues:**
- Series of `cmp` + `jb`/`jl`/`jge` instruction pairs in the hot path
- No lookup load instructions; all logic is done through conditional jumps
- Compiler-generated jump tables (`jmp [table + reg*8]`) are also branch-based and still mispredict for uniform data

## Transformation

### Strategy 1: Direct lookup table (small input range)

Precompute the mapping for every possible input value and store in an array. Replace the entire if/else chain with a single bounds check and array access.

**perf-book example** (simplified buckets of size 10):

```cpp
// Before: if/else chain with 5 branches
int8_t mapToBucket(unsigned v) {
    if      (v < 10) return 0;
    else if (v < 20) return 1;
    else if (v < 30) return 2;
    else if (v < 40) return 3;
    else if (v < 50) return 4;
    return -1;
}
```

```cpp
// After: single array lookup -- one branch (bounds check), one load
static constexpr int8_t buckets[50] = {
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0,  // [0,10)  -> bucket 0
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1,  // [10,20) -> bucket 1
    2, 2, 2, 2, 2, 2, 2, 2, 2, 2,  // [20,30) -> bucket 2
    3, 3, 3, 3, 3, 3, 3, 3, 3, 3,  // [30,40) -> bucket 3
    4, 4, 4, 4, 4, 4, 4, 4, 4, 4,  // [40,50) -> bucket 4
};

int8_t mapToBucket(unsigned v) {
    if (v < 50)
        return buckets[v];
    return -1;
}
```

The remaining `if (v < 50)` branch is well-predicted because most values fall within range. The hot path is: one comparison (predicted taken) + one load from L1 cache.

### Strategy 2: LUT for the perf-ninja histogram problem

For the non-uniform bucket boundaries in the perf-ninja lab:

```cpp
// Before: 7-deep if/else chain called 1M times
static std::size_t mapToBucket(std::size_t v) {
    if      (v < 13)  return 0;
    else if (v < 29)  return 1;
    else if (v < 41)  return 2;
    else if (v < 53)  return 3;
    else if (v < 71)  return 4;
    else if (v < 83)  return 5;
    else if (v < 100) return 6;
    return DEFAULT_BUCKET;
}
```

```cpp
// After: precomputed lookup table covering [0, 100)
static constexpr std::size_t LUT[100] = {
    // [0,13) -> 0
    0,0,0,0,0,0,0,0,0,0,0,0,0,
    // [13,29) -> 1
    1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,
    // [29,41) -> 2
    2,2,2,2,2,2,2,2,2,2,2,2,
    // [41,53) -> 3
    3,3,3,3,3,3,3,3,3,3,3,3,
    // [53,71) -> 4
    4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,
    // [71,83) -> 5
    5,5,5,5,5,5,5,5,5,5,5,5,
    // [83,100) -> 6
    6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,
};

static std::size_t mapToBucket(std::size_t v) {
    if (v < 100)
        return LUT[v];
    return DEFAULT_BUCKET;
}
```

### Strategy 3: Arithmetic replacement (when pattern is regular)

From perf-book Ch.10: if bucket boundaries follow a regular pattern, replace the entire mapping with arithmetic.

```cpp
// Before: branches for regular 10-wide buckets
int8_t mapToBucket(unsigned v) {
    if (v < 50)
        return v / 10;
    return -1;
}
```

The compiler optimizes `v / 10` into a multiply-and-shift sequence (no division instruction), so the hot path is: one comparison + one multiply + one shift. No branches in the computation.

### Strategy 4: Interval map for large ranges

For ranges too large for a flat LUT (e.g., `[0, 1M)`), use an interval map data structure with O(log N) lookup:

- Boost `interval_map`: `boost::icl::interval_map`
- LLVM `IntervalMap`: `llvm::IntervalMap`

These trade some lookup latency for dramatically reduced memory usage compared to a multi-megabyte flat table.

## Expected Impact

- **Branch elimination:** An N-way if/else chain generates up to N branches. A LUT replaces them with 1 well-predicted bounds check + 1 memory load.
- **Misprediction savings:** For uniform distribution over 7 buckets, expected mispredictions per call is ~3-4 (each comparison ~50/50). At 15-20 cycles per mispredict (x86), that is 45-80 wasted cycles per call. The LUT version costs ~4-5 cycles (L1 hit + bounds check).
- **perf-ninja lookup_tables_1:** With 1M calls, eliminating the if/else chain can yield 3-5x speedup.
- **Memory cost:** A 100-byte LUT is trivial. Even a 4KB table fits comfortably in L1 data cache (typically 32-48 KB).

## Caveats

- **LUT must fit in L1 cache.** If the lookup table exceeds L1 D-cache size (32-48 KB), cache misses on the table itself can negate the branch elimination benefit. For a `uint8_t` table, the practical limit is ~32K entries before L1 pressure becomes a concern.
- **Do NOT use for well-predicted branches.** If input values are clustered or sorted (e.g., mostly falling into one bucket), the branch predictor handles the if/else chain efficiently. The LUT adds a memory access that may be slower than a correctly predicted branch.
- **Memory vs. computation tradeoff.** Each byte in the LUT competes for cache space with other hot data. In memory-bound code, adding a LUT may evict useful data and hurt overall performance.
- **Maintenance burden.** If bucket boundaries change, the LUT must be regenerated. Consider generating LUT contents programmatically at compile time (`constexpr` function) rather than hand-coding.
- **Input validation.** The LUT approach still requires a bounds check for out-of-range values. Failing to guard against out-of-bounds access is a correctness and security bug.
- **Large input ranges.** For ranges > 10K, a flat LUT wastes memory. Use interval maps, binary search, or arithmetic formulas instead.
- **Cold start.** On the very first access, the LUT may not be in cache and incurs a cache miss. This is irrelevant for hot loops but matters for rarely-called functions.
