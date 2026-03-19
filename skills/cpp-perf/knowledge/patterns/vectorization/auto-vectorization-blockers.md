---
name: Auto-Vectorization Blockers
source: perf-book Ch.9, perf-ninja core_bound/vectorization_1, vectorization_2
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [loop, vectorization, SIMD, scalar, auto-vectorize, restrict, alias, stride]
---

## Problem

Loops that should benefit from SIMD execution remain scalar because the compiler cannot prove vectorization is safe, profitable, or legal. Common blockers include pointer aliasing, non-unit stride access, loop-carried dependencies, floating-point reassociation constraints, function calls in loop bodies, and complex control flow.

## Detection

### Compiler diagnostics

Use compiler optimization reports to identify why vectorization failed:

```bash
# Clang
clang++ -O3 -Rpass-missed=loop-vectorize -Rpass-analysis=loop-vectorize src.cpp

# GCC
g++ -O3 -fopt-info-vec-missed src.cpp
```

Common diagnostic messages and their meanings:

| Message | Blocker |
|---------|---------|
| `cannot prove it is safe to reorder floating-point operations` | FP reassociation |
| `loop versioned for vectorization because of possible aliasing` | Pointer aliasing |
| `the cost-model indicates that vectorization is not beneficial` | Non-unit stride / scatter-gather |
| `loop not vectorized: loop contains a switch statement` | Complex control flow |
| `value that could not be identified as a reduction` | Loop-carried dependency |

### Assembly-level detection

On x86, vectorized code uses packed instructions (`VMULPS`, `VADDPS`) with `XMM`/`YMM`/`ZMM` registers. Scalar code uses single-element instructions (`VMULSS`, `VADDSS`). Do not be fooled by seeing `XMM` registers alone -- `VMULSS XMM1, XMM2, XMM3` is scalar despite using XMM.

On ARM, vectorized code uses NEON `v` registers with multi-element operations (`fmul v0.4s, v1.4s, v2.4s`) versus scalar `s`/`d` register operations.

A high TMA `Retiring` metric (above 80%) that does not translate to high throughput may indicate that scalar instructions dominate where vector instructions should be used.

## Transformation

### Blocker 1: Pointer aliasing

When the compiler cannot prove that pointer arguments do not overlap, it must either generate runtime alias checks (adding overhead) or fall back to scalar code.

**Before** (from perf-book, GCC generates versioned loop with runtime checks):
```cpp
void foo(float* a, float* b, float* c, unsigned N) {
  for (unsigned i = 1; i < N; i++) {
    c[i] = b[i];
    a[i] = c[i-1];
  }
}
// GCC: "loop versioned for vectorization because of possible aliasing"
```

**After** (annotate with `__restrict__` to eliminate runtime checks):
```cpp
void foo(float* __restrict__ a, float* __restrict__ b,
         float* __restrict__ c, unsigned N) {
  for (unsigned i = 1; i < N; i++) {
    c[i] = b[i];
    a[i] = c[i-1];
  }
}
```

Alternative: use `#pragma GCC ivdep` (GCC) or `#pragma clang loop vectorize(assume_safety)` before the loop.

### Blocker 2: Non-unit stride access (scatter/gather)

Strided memory access patterns force the compiler to generate expensive gather/scatter operations, often causing it to reject vectorization entirely.

**Before** (from perf-book, Clang rejects as unprofitable):
```cpp
void stridedLoads(int *A, int *B, int n) {
  for (int i = 0; i < n; i++)
    A[i] += B[i * 3];  // stride-3 access on B
}
// Clang: "the cost-model indicates that vectorization is not beneficial"
```

**After** (restructure data for contiguous access, or force with pragma if profiling confirms benefit):
```cpp
void stridedLoads(int *A, int *B, int n) {
#pragma clang loop vectorize(enable)
  for (int i = 0; i < n; i++)
    A[i] += B[i * 3];
}
```

The better fix is to restructure data layout so accesses are contiguous (SoA instead of AoS, pre-gather into a contiguous buffer, etc.).

### Blocker 3: Loop-carried dependency preventing vectorization

**Before** (read-after-write dependency -- each iteration depends on the previous):
```cpp
void vectorDependence(int *A, int n) {
  for (int i = 1; i < n; i++)
    A[i] = A[i-1] * 2;  // RAW dependency: cannot parallelize
}
```

This cannot be vectorized as-is. The fix requires algorithmic restructuring:

**After** (recognize the closed-form: `A[i] = A[0] * 2^i`):
```cpp
void vectorDependence(int *A, int n) {
  int base = A[0];
  for (int i = 1; i < n; i++)
    A[i] = base << i;  // no dependency between iterations
}
```

### Blocker 4: Floating-point reassociation (from perf-book)

**Before** (compiler refuses to vectorize sum reduction because FP addition is not associative):
```cpp
float calcSum(float* a, unsigned N) {
  float sum = 0.0f;
  for (unsigned i = 0; i < N; i++) {
    sum += a[i];
  }
  return sum;
}
// Clang: "cannot prove it is safe to reorder floating-point operations"
```

**After** (use pragma or `-ffast-math` to allow reassociation):
```cpp
float calcSum(float* a, unsigned N) {
  float sum = 0.0f;
#pragma clang fp reassociate(on)
  for (unsigned i = 0; i < N; i++) {
    sum += a[i];
  }
  return sum;
}
// Result: "vectorized loop (vectorization width: 4, interleaved count: 2)"
```

The scoped `#pragma clang fp reassociate(on)` (Clang 18+) is safer than global `-ffast-math`, which also affects NaN, signed zero, infinity, and subnormal handling.

### Blocker 5: Add-carry pattern preventing vectorization (perf-ninja vectorization_2)

**Before** (the `acc < value` comparison creates a loop-carried dependency that blocks vectorization):
```cpp
uint16_t checksum(const Blob &blob) {
  uint16_t acc = 0;
  for (auto value : blob) {
    acc += value;
    acc += acc < value;  // add carry -- blocks vectorization
  }
  return acc;
}
```

**After** (widen to 32-bit to avoid carry, fold carry at the end per RFC 1071):
```cpp
uint16_t checksum(const Blob &blob) {
  uint32_t acc = 0;  // wider accumulator -- no carry needed per iteration
  for (auto value : blob) {
    acc += value;
  }
  // Fold 32-bit into 16-bit with carry
  while (acc > 0xFFFF)
    acc = (acc & 0xFFFF) + (acc >> 16);
  return static_cast<uint16_t>(acc);
}
```

By widening the accumulator, the loop body becomes a simple reduction that the compiler can auto-vectorize. The carry folding happens once after the loop instead of on every iteration.

## Expected Impact

| Blocker Removed | Typical Speedup |
|----------------|-----------------|
| Pointer aliasing (adding `__restrict__`) | 1.5-3x (eliminates runtime alias checks) |
| FP reassociation (pragma/fast-math) | 2-8x (enables SIMD width 4-8 with interleaving) |
| Carry dependency widening (vectorization_2) | 3-10x (entire loop becomes vectorizable) |
| Non-unit stride removal | 2-4x (if data layout can be changed) |
| Forced vectorization via pragma | Variable -- must profile; can be negative |

## Caveats

- **`__restrict__` is a promise**: if pointers actually alias, behavior is undefined. Only use when you can guarantee non-overlap.
- **`-ffast-math` is dangerous at scale**: it changes semantics for NaN, infinity, signed zero, and subnormals. Prefer scoped pragmas (`#pragma clang fp reassociate(on)`) over global flags. Never enable across an entire large codebase without validation.
- **Loop-carried dependencies may be fundamental**: not all can be broken. Recognize when the algorithm inherently requires sequential execution (e.g., prefix-sum dependencies without the parallel prefix-sum algorithm).
- **Forced vectorization can hurt**: `#pragma clang loop vectorize(enable)` overrides the cost model. If the loop has very few iterations, the vectorized version with its setup/remainder overhead may be slower than scalar.
- **AVX-512 frequency throttling**: on certain x86 CPUs, heavy AVX-512 usage causes frequency downclocking that can persist for microseconds and affect subsequent code. The vectorized portion must be hot enough to amortize this (sorting >= 80 KiB was found sufficient).
- **Compiler upgrades can break auto-vectorization**: code that vectorized in one compiler version may stop vectorizing in the next. Consider adding `static_assert` or compile-time checks in performance-critical paths.
