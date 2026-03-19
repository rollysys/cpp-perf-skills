---
name: Cache Set Aliasing (Power-of-2 Conflict Misses)
source: perf-book Ch.12, Agner Fog Optimizing Software Ch.9
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [cache aliasing, set conflict, power of two, matrix, padding, cache set, eviction, associativity]
---

## Problem

When matrix dimensions are exact powers of 2, column accesses map to the same cache sets, causing **conflict misses** that bypass the cache's associativity. This can degrade performance by 10x or more compared to non-power-of-2 dimensions.

The root cause lies in how set-associative caches map addresses to sets. For an N-way set-associative L1 cache with S sets and line size L:

```
cache_set = (address / L) % S
```

If a matrix has N columns where N is a power of 2, then column elements in consecutive rows are separated by exactly `N * sizeof(element)` bytes. If this stride is a multiple of `S * L` (the cache size), all column elements map to the **same cache set**. With only N-way associativity (typically 8-way for L1D), accessing more than N rows in the same column causes cascading evictions.

**Example:** Consider a `double matrix[512][512]` with a 32KB 8-way L1D cache (64-byte lines, 64 sets):
- Row stride = 512 * 8 = 4096 bytes
- `4096 / 64 = 64` -- this is exactly the number of sets in the cache
- Every row's column 0 maps to the same cache set
- Accessing column 0 across 9+ rows evicts data on every access (only 8 ways available)

This is catastrophic for column-major traversals, matrix transpose, and any algorithm that accesses elements in a strided pattern with a power-of-2 stride.

## Detection

**Source-level indicators:**
- Matrix/array dimensions that are powers of 2: 256, 512, 1024, 2048, 4096
- Column-major traversal of row-major matrices (or vice versa)
- Matrix transpose operations
- Stencil computations accessing multiple rows at power-of-2 offsets

**Profile-level indicators:**
```bash
# x86: watch for L1D replacements disproportionate to working set size
perf stat -e L1-dcache-load-misses,L1-dcache-loads ./myapp

# Compare runs with N=1024 vs N=1025 -- if the latter is significantly faster,
# cache aliasing is the culprit
```

- TMA: high `Memory_Bound > L1_Bound > Store_Latency` or unexpected L1D miss rates
- **Characteristic symptom:** performance cliff when matrix dimension crosses a power-of-2 boundary, with N=1024 being much slower than N=1023 or N=1025

**Quick diagnostic test:**
```cpp
// Run the same algorithm with N and N+1 -- if N+1 is significantly faster,
// cache aliasing is confirmed
benchmark(matrix_op, N=1024);  // slow
benchmark(matrix_op, N=1025);  // fast -- cache aliasing confirmed
```

## Transformation

### Strategy 1: Pad columns to break alignment

Add one or more extra columns so the row stride is no longer a power-of-2 multiple of the cache set count:

```cpp
// Before: power-of-2 stride causes aliasing
constexpr int N = 1024;
double matrix[N][N];  // row stride = 8192 bytes, aliases every 64 sets

// After: pad with +1 column to break aliasing
constexpr int N = 1024;
constexpr int N_PAD = N + 1;  // or N + 8 for extra safety
double matrix[N][N_PAD];      // row stride = 8200 bytes, no aliasing

// Access unchanged -- just ignore the padding column
for (int j = 0; j < N; j++)
    for (int i = 0; i < N; i++)
        sum += matrix[i][j];  // column traversal now conflict-free
```

For dynamic allocation:

```cpp
int N = 1024;
int stride = N + 1;  // padded stride
std::vector<double> matrix(N * stride);

auto at = [&](int row, int col) -> double& {
    return matrix[row * stride + col];
};
```

### Strategy 2: Cache blocking (tiling)

Process the matrix in small blocks that fit entirely in L1 cache, avoiding cross-set conflicts:

```cpp
constexpr int BLOCK = 64;  // block size tuned for L1 cache

// Matrix transpose with blocking
void transpose(double dst[][N], const double src[][N], int N) {
    for (int ii = 0; ii < N; ii += BLOCK)
        for (int jj = 0; jj < N; jj += BLOCK)
            for (int i = ii; i < std::min(ii + BLOCK, N); i++)
                for (int j = jj; j < std::min(jj + BLOCK, N); j++)
                    dst[j][i] = src[i][j];
}
```

Blocking ensures that within each block, the accessed addresses span enough different cache sets to avoid saturation.

### Strategy 3: Non-power-of-2 allocation policy

As an architectural pattern, always allocate matrices with non-power-of-2 strides:

```cpp
// Utility: compute padded stride that avoids cache aliasing
constexpr int safe_stride(int cols, int elem_size, int cache_line = 64) {
    int stride_bytes = cols * elem_size;
    // If stride is a multiple of cache_line * num_sets, add padding
    if ((stride_bytes & (stride_bytes - 1)) == 0 ||  // power of 2
        stride_bytes % (cache_line * 64) == 0) {       // multiple of cache size
        return cols + cache_line / elem_size;
    }
    return cols;
}

int stride = safe_stride(1024, sizeof(double));  // returns 1032
```

## Expected Impact

- **Column traversal of power-of-2 matrices:** 2-10x speedup by eliminating conflict misses. The exact factor depends on how many rows are accessed (more rows = more ways exceeded = worse degradation).
- **Matrix transpose:** 3-5x speedup for power-of-2 dimensions with padding or blocking.
- **The fix is often free:** padding adds minimal memory overhead (one extra element per row) and requires no algorithmic changes.
- **Cache blocking:** provides benefit beyond just aliasing -- also improves spatial locality for non-power-of-2 sizes, though the improvement is smaller.

## Caveats

- **Only affects power-of-2 (or cache-aligned) strides:** non-power-of-2 dimensions rarely cause significant aliasing. Always verify with profiling before adding padding.
- **L2 and L3 have more sets:** aliasing that is catastrophic for L1 (64 sets) may be benign for L2 (1024+ sets). However, L1 misses still cost 5-12 cycles each, which dominates in tight loops.
- **Padding wastes memory:** one extra column per row. For a 4096x4096 double matrix, padding adds 32KB (4096 * 8 bytes) -- trivial overhead.
- **BLAS/LAPACK libraries handle this internally:** production-quality linear algebra libraries already use blocking and non-power-of-2 internal strides. This pattern is most relevant for hand-written matrix code.
- **ARM L1D specifics:** ARM Cortex-A76 has 64KB 4-way L1D (256 sets, 64-byte lines). The aliasing threshold is different from x86 (32KB 8-way, 64 sets). Always compute the set count for your target.
- **Compiler may pad automatically:** some compilers with `-O3` or PGO may detect and mitigate aliasing. Check generated array layouts before manually padding.
