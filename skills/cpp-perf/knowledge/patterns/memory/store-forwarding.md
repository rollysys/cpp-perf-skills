---
name: Memory Order Violation / Store Forwarding Conflict
source: perf-ninja memory_bound/mem_order_violation_1
layers: [microarch, source]
platforms: [arm, x86]
keywords: [store forwarding, memory order violation, histogram, store-to-load, multiple histograms, accumulator splitting]
---

## Problem

When computing a histogram, rapid read-modify-write of the same memory location causes a **memory order violation** (also called store forwarding stall). The CPU pipeline speculatively loads `hist[x]` for pixel `i+1` before the store from pixel `i` has committed. If both pixels map to the same histogram bin, the load gets stale data and must be replayed.

For images where consecutive pixels often have similar values (real photographs), this causes massive pipeline stalls. The store buffer cannot forward the result fast enough.

```cpp
std::array<uint32_t, 256> hist;
hist.fill(0);
for (int i = 0; i < image.width * image.height; ++i)
  hist[image.data[i]]++;  // read-modify-write: load, add 1, store
```

## Detection

- Profile shows high "memory order violation" or "store forwarding" stalls
- Histogram/counting pattern: `array[data[i]]++` in a tight loop
- On Intel: `MACHINE_CLEARS.MEMORY_ORDERING` counter is elevated
- Small output array (e.g., 256 bins) with high collision rate from input data
- TMA shows "Memory Bound > Store Bound" as bottleneck

## Transformation

**Before** (from solution.cpp -- single histogram):
```cpp
std::array<uint32_t, 256> computeHistogram(const GrayscaleImage& image) {
  std::array<uint32_t, 256> hist;
  hist.fill(0);
  for (int i = 0; i < image.width * image.height; ++i)
    hist[image.data[i]]++;
  return hist;
}
```

**After** (multiple histograms to break store-to-load dependencies):
```cpp
std::array<uint32_t, 256> computeHistogram(const GrayscaleImage& image) {
  // Use 4 independent histograms to avoid store forwarding conflicts
  constexpr int NUM_HISTS = 4;
  std::array<std::array<uint32_t, 256>, NUM_HISTS> hists;
  for (auto& h : hists) h.fill(0);

  int total = image.width * image.height;
  int i = 0;

  // Process 4 pixels per iteration, each updating a different histogram
  for (; i + NUM_HISTS <= total; i += NUM_HISTS) {
    hists[0][image.data[i + 0]]++;
    hists[1][image.data[i + 1]]++;
    hists[2][image.data[i + 2]]++;
    hists[3][image.data[i + 3]]++;
  }

  // Scalar tail
  for (; i < total; ++i)
    hists[0][image.data[i]]++;

  // Merge histograms
  std::array<uint32_t, 256> result;
  for (int b = 0; b < 256; ++b)
    result[b] = hists[0][b] + hists[1][b] + hists[2][b] + hists[3][b];

  return result;
}
```

Key insight: by distributing consecutive pixels across separate histogram arrays, two consecutive operations almost never alias the same memory location, eliminating the store forwarding conflict.

## Expected Impact

- 2-4x speedup for histogram computation on typical image data
- Eliminates nearly all memory order violation pipeline flushes
- Extra memory cost: (NUM_HISTS - 1) * 256 * 4 bytes = 3KB for 4 histograms

## Caveats

- Number of histograms (NUM_HISTS) should be tuned: 4 is usually sufficient, 8 may help on some microarchitectures but uses more cache
- If input data has high entropy (every pixel is different), the single-histogram version has few collisions and this optimization has minimal benefit
- The merge step adds overhead; for very small images it may not be worthwhile
- On ARM, the store forwarding behavior may differ; test with actual profiling data
- Alternative: process strided (e.g., every 4th pixel in each pass) to achieve the same effect
- Total extra memory: NUM_HISTS * 1KB, which fits in L1 cache
