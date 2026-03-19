---
name: Loop Tiling for Cache Locality
source: perf-book Ch.8, perf-ninja memory_bound/loop_tiling_1
layers: [microarchitecture, system]
platforms: [arm, x86]
keywords: [nested loop, 2D array, matrix, stride, cache miss, working set, tile, block]
---

## Problem

When a nested loop accesses a 2D array with a column-major (stride-N) access pattern, each inner iteration touches a different cache line. By the time the loop revisits the same row, the previously fetched cache line has already been evicted. This results in massive cache thrashing proportional to the matrix dimension.

Classic example: matrix transpose where `out[i][j] = in[j][i]` -- the read from `in[j][i]` strides through column `i`, hitting a new cache line on every increment of `j`.

```cpp
// Naive matrix transpose -- column-wise reads cause cache thrashing
using MatrixOfDoubles = std::vector<std::vector<double>>;

bool solution(MatrixOfDoubles &in, MatrixOfDoubles &out) {
  int size = in.size();
  for (int i = 0; i < size; i++) {
    for (int j = 0; j < size; j++) {
      out[i][j] = in[j][i];  // in[j][i] strides by `size` doubles per j++
    }
  }
  return out[0][size - 1];
}
```

For a 512x512 matrix of doubles (8 bytes each), each row is 4 KB. The inner loop over `j` touches 512 different cache lines from `in`, far exceeding L1 capacity (typically 32-48 KB).

## Detection

**Source-level indicators:**
- Nested loops over 2D arrays where the inner loop index is used as the outer array subscript (e.g., `arr[j][i]` where `j` is the inner loop variable)
- Matrix operations: transpose, multiplication, convolution
- Working set of inner loop exceeds L1/L2 cache size

**Profile-level indicators:**
- High L1d cache miss rate (> 10%) on load instructions inside the inner loop
- TMA: high `Memory_Bound > L1_Bound` or `L2_Bound`
- `perf stat`: high `L1-dcache-load-misses` / `L1-dcache-loads` ratio

**Disassembly clues:**
- Load addresses incrementing by a large stride (row_size * element_size) between iterations

## Transformation

Split the iteration space into tiles (blocks) that fit in L1 or L2 cache. Process each tile completely before moving to the next.

```cpp
// Before: naive transpose
for (int i = 0; i < size; i++) {
  for (int j = 0; j < size; j++) {
    out[i][j] = in[j][i];
  }
}
```

```cpp
// After: tiled transpose
constexpr int TILE = 32;  // chosen so TILE * TILE * sizeof(double) fits L1

for (int ii = 0; ii < size; ii += TILE) {
  for (int jj = 0; jj < size; jj += TILE) {
    // Process one tile
    int i_end = std::min(ii + TILE, size);
    int j_end = std::min(jj + TILE, size);
    for (int i = ii; i < i_end; i++) {
      for (int j = jj; j < j_end; j++) {
        out[i][j] = in[j][i];
      }
    }
  }
}
```

**Choosing TILE size:**

The tile working set must fit in L1 cache. For a transpose:
- Working set per tile: `TILE * TILE * sizeof(element) * 2` (read tile + write tile)
- L1 cache is typically 32-48 KB
- For `double` (8 bytes): `32 * 32 * 8 * 2 = 16 KB` -- fits comfortably in L1
- For `float` (4 bytes): `64 * 64 * 4 * 2 = 32 KB` -- fits in 48 KB L1

For matrix multiplication, the analysis is different because three matrices are involved:
- Tile of A: `TILE * TILE * sizeof(element)`
- Column strip of B: reused across the tile
- Tile of C: `TILE * TILE * sizeof(element)`
- Target L2 if the full working set does not fit L1

**Platform-specific tile sizes:**
| Platform | L1d size | Suggested TILE (double) | Suggested TILE (float) |
|----------|----------|------------------------|----------------------|
| ARM Cortex-A7x | 32-64 KB | 32-48 | 48-64 |
| Intel/AMD x86 | 32-48 KB | 32 | 48-64 |

The optimal tile size is experimental. Use cache profiling to verify improvement.

## Expected Impact

- **Cache miss reduction:** From O(N^2) L1 misses down to O(N^2 / TILE) misses for the strided dimension.
- **Typical speedup:** 2-10x for large matrices (N > 256), depending on how badly the original code thrashes the cache.
- **perf-ninja loop_tiling_1:** the tiled solution shows significant speedup for matrix transpose of size 512x512+.
- **Diminishing returns:** for small matrices that already fit in L1 (N < 64 for doubles), tiling adds overhead with no benefit.

## Caveats

- **Small matrices:** if `N * N * sizeof(element)` fits in L1 cache, tiling is unnecessary and adds loop overhead.
- **Tile size is platform-dependent:** a tile size optimized for one CPU's L1 may be suboptimal on another. Parameterize and benchmark.
- **Code complexity:** tiling adds 2 extra loop levels and boundary handling (`std::min`). For simple loops this is manageable; for complex loop bodies it may hurt readability.
- **Compiler auto-tiling:** some compilers (ICC/ICX, GCC with `-floop-block`) can perform loop tiling automatically. Check the optimization report before manual intervention.
- **Interaction with vectorization:** ensure the inner tile loop is still vectorizable. The tiled inner loop should iterate over contiguous memory for the write side.
- **Non-square tiles:** for operations like matrix multiplication where access patterns differ per matrix, rectangular tiles may be optimal.
