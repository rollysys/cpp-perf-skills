---
name: Conditional Store Elimination
source: perf-ninja bad_speculation/conditional_store_1
layers: [source, compiler]
platforms: [arm, x86]
keywords: [branch misprediction, conditional store, branchless, selection, filtering, CMOV]
---

## Problem

When filtering/selecting elements from a large array based on a condition with random data, the branch is highly unpredictable. The CPU's branch predictor fails ~50% of the time, causing pipeline flushes on each misprediction (~15-20 cycles penalty on modern CPUs).

The pattern: iterate over input, conditionally copy matching elements to output. With random keys, the branch `if (lower <= item.first && item.first <= upper)` is unpredictable.

## Detection

- Profiler shows high branch misprediction rate (>10%) on a filtering/selection loop
- Code pattern: `if (condition) output[count++] = input[i];`
- Input data has low predictability (random, or pattern not learnable by branch predictor)
- TMA shows "Bad Speculation" as a significant bottleneck

## Transformation

**Before** (from init.cpp -- conditional store with branch):
```cpp
std::size_t select(std::array<S, N> &output, const std::array<S, N> &input,
                   const std::uint32_t lower, const std::uint32_t upper) {
  std::size_t count = 0;
  for (const auto item : input) {
    if ((lower <= item.first) && (item.first <= upper)) {
      output[count++] = item;
    }
  }
  return count;
}
```

**After** (branchless -- always store, conditionally increment):
```cpp
std::size_t select(std::array<S, N> &output, const std::array<S, N> &input,
                   const std::uint32_t lower, const std::uint32_t upper) {
  std::size_t count = 0;
  for (const auto item : input) {
    // Always store (may overwrite on next non-matching iteration)
    output[count] = item;
    // Branchless conditional increment
    count += (lower <= item.first) && (item.first <= upper);
  }
  return count;
}
```

Key insight: we always write `item` to `output[count]`, but only increment `count` when the condition is true. If the condition is false, the next iteration overwrites the same slot. This eliminates the branch entirely.

## Expected Impact

- 2-5x speedup when branch misprediction rate is high (random data)
- Eliminates all branch mispredictions in the filtering loop
- Trades extra (unconditional) stores for eliminated pipeline flushes

## Caveats

- Only beneficial when branch is truly unpredictable; if data is mostly matching or mostly non-matching, the branch predictor works well and the branchless version adds unnecessary stores
- The unconditional store may touch more cache lines (write amplification)
- If the stored type is very large, the cost of redundant stores may exceed branch misprediction cost
- Output array must have capacity >= input array size (since we speculatively write)
- For trivially-copyable small types (like pairs of uint32_t), this is almost always a win with random data
