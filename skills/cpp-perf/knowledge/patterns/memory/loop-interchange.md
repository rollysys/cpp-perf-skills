---
name: Loop Interchange for Spatial Locality
source: perf-ninja memory_bound/loop_interchange_1, memory_bound/loop_interchange_2
layers: [source]
platforms: [arm, x86]
keywords: [loop interchange, cache miss, spatial locality, matrix multiplication, column traversal, row-major, stride]
---

## Problem

When nested loops access a 2D array with the wrong loop order, the inner loop strides across rows (large stride) instead of along columns within a row (unit stride). This destroys spatial locality: each access touches a different cache line, causing massive L1/L2 cache misses.

For a 400x400 float matrix (640KB), column-major traversal in a row-major layout means every access is 1600 bytes apart -- no cache line reuse.

## Detection

- Profile shows high LLC/L2 cache miss rate in a nested loop over 2D arrays
- Inner loop index matches the first (row) dimension: `array[k][j]` where `k` is the inner loop variable
- Matrix multiply with `i,j,k` order where innermost `k` accesses `b[k][j]` (column traversal of b)
- Gaussian blur / image filter processing columns in outer loop, rows in inner loop
- TMA shows "Memory Bound" as dominant bottleneck

## Transformation

### Example 1: Matrix Multiplication (loop_interchange_1)

**Before** (from solution.cpp -- i,j,k order with column access on b):
```cpp
void multiply(Matrix &result, const Matrix &a, const Matrix &b) {
  zero(result);
  for (int i = 0; i < N; i++) {
    for (int j = 0; j < N; j++) {
      for (int k = 0; k < N; k++) {
        result[i][j] += a[i][k] * b[k][j];  // b[k][j]: k varies, stride = N
      }
    }
  }
}
```

**After** (i,k,j order -- sequential access on both result and b):
```cpp
void multiply(Matrix &result, const Matrix &a, const Matrix &b) {
  zero(result);
  for (int i = 0; i < N; i++) {
    for (int k = 0; k < N; k++) {
      for (int j = 0; j < N; j++) {
        result[i][j] += a[i][k] * b[k][j];  // j varies: result[i][j] and b[k][j] both sequential
      }
    }
  }
}
```

### Example 2: Gaussian Blur Vertical Filter (loop_interchange_2)

**Before** (from solution.cpp -- outer loop over columns, inner over rows):
```cpp
static void filterVertically(uint8_t *output, const uint8_t *input,
                             const int width, const int height, ...) {
  for (int c = 0; c < width; c++) {          // outer: column
    for (int r = radius; r < height - radius; r++) {  // inner: row
      int dot = 0;
      for (int i = 0; i < radius + 1 + radius; i++) {
        dot += input[(r - radius + i) * width + c] * kernel[i];
      }
      output[r * width + c] = static_cast<uint8_t>((dot + rounding) >> shift);
    }
  }
}
```

**After** (swap loop order -- outer over rows, inner over columns):
```cpp
static void filterVertically(uint8_t *output, const uint8_t *input,
                             const int width, const int height, ...) {
  for (int r = radius; r < height - radius; r++) {    // outer: row
    for (int c = 0; c < width; c++) {                  // inner: column
      int dot = 0;
      for (int i = 0; i < radius + 1 + radius; i++) {
        dot += input[(r - radius + i) * width + c] * kernel[i];
      }
      output[r * width + c] = static_cast<uint8_t>((dot + rounding) >> shift);
    }
  }
}
```

Now `c` varies in the inner loop, so both `input[... + c]` and `output[... + c]` access sequential memory.

## Expected Impact

- 3-10x speedup for matrix multiply (N=400)
- 2-5x speedup for image filters
- Reduces L1 cache misses by 10-100x depending on matrix/image size
- Effect scales with data size: larger arrays = more dramatic improvement

## Caveats

- Loop interchange is only valid when there are no loop-carried dependencies that change semantics
- For matrix multiply, the i,k,j order changes the accumulation pattern (sum order) but not the result (floating-point associativity may cause tiny numerical differences)
- If the array fits entirely in L1 cache, loop order has negligible impact
- The compiler may auto-interchange loops at high optimization levels (-O3), but frequently fails for non-trivial loop bodies
- For very small matrices (< 16x16), the overhead of loop control may outweigh cache benefits
- Consider loop tiling (blocking) for even better cache utilization on very large matrices
