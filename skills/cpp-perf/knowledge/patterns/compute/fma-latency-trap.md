---
name: FMA Latency Trap on Dependency Chains
source: perf-book Ch.12, Agner Fog instruction tables
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [FMA, fused multiply-add, latency, dependency chain, contract, critical path, mul add split]
---

## Problem

Fused Multiply-Add (FMA) is conventionally seen as a pure win -- it replaces two operations with one instruction and eliminates an intermediate rounding. However, when the FMA sits on a **loop-carried dependency chain**, it can actually be *slower* than separate multiply + add.

The root cause is latency arithmetic on the critical path:

- **FMA latency = 4 cycles** (on most ARM and Intel cores). The result, including the addition, is not available for 4 cycles.
- **Separate MUL latency = 3 cycles, ADD latency = 1 cycle.** But critically, the ADD only depends on the MUL result, not on the previous iteration's accumulator until the MUL completes. In an out-of-order engine, the MUL for iteration N+1 can begin before the ADD from iteration N finishes.

Consider a sum-of-squares reduction: `sum += a[i] * a[i]`

With FMA (compiler contracts mul+add into `fmadd`):
```
iter 0: fmadd sum, a[0], a[0], sum   ; sum available at cycle 4
iter 1: fmadd sum, a[1], a[1], sum   ; starts at cycle 4, done at cycle 8
iter 2: fmadd sum, a[2], a[2], sum   ; starts at cycle 8, done at cycle 12
Critical path: 4 cycles/iteration
```

With separate MUL + ADD:
```
iter 0: fmul tmp0, a[0], a[0]        ; tmp0 available at cycle 3
        fadd sum, sum, tmp0           ; sum available at cycle 4
iter 1: fmul tmp1, a[1], a[1]        ; starts at cycle 0 (independent!), done at cycle 3
        fadd sum, sum, tmp1           ; starts at cycle 4 (waits for sum), done at cycle 5
iter 2: fmul tmp2, a[2], a[2]        ; starts at cycle 0, done at cycle 3
        fadd sum, sum, tmp2           ; starts at cycle 5, done at cycle 6
Critical path: the ADD chain is 1 cycle/iteration (after initial fill)
```

The separate-ops version has a critical path of 1-cycle ADD latency per iteration (the MULs are off the critical path and execute in parallel). With FMA, the critical path is 4 cycles per iteration. This is a **4x difference** in the dependency chain bottleneck.

## Detection

**Source-level indicators:**
- Reduction loop with a single accumulator: `sum += expr * expr` or `sum += a[i] * b[i]`
- Any loop where the FMA result feeds back into the next iteration's FMA as the addend
- Horner polynomial evaluation on a single accumulator: `r = r * x + c` (each FMA depends on previous `r`)

**Assembly-level indicators (ARM):**
```asm
; Suspicious: fmadd on a loop-carried register
.loop:
  ldr   s1, [x0], #4
  fmadd s0, s1, s1, s0    ; s0 depends on previous s0 -- 4-cycle chain
  subs  x1, x1, #1
  b.ne  .loop
```

**Profile-level indicators:**
- Low IPC on a tight loop that should be throughput-bound
- TMA showing `Core Bound > Ports Utilization` despite simple FP operations
- Loop throughput matching FMA latency (4 cycles/iter) rather than ADD throughput (1 cycle/iter)

## Transformation

### Strategy 1: Disable FP contraction with pragma

Prevent the compiler from fusing mul+add into FMA in the critical loop:

```cpp
// Before: compiler contracts into fmadd, 4-cycle critical path
float sum_of_squares(const float* a, int n) {
    float sum = 0.0f;
    for (int i = 0; i < n; i++) {
        sum += a[i] * a[i];  // fmadd: 4 cycles on critical path
    }
    return sum;
}

// After: force separate mul + add, 1-cycle critical path (mul is off-chain)
float sum_of_squares(const float* a, int n) {
    float sum = 0.0f;
    #pragma clang fp contract(off)
    for (int i = 0; i < n; i++) {
        float tmp = a[i] * a[i];  // fmul: off critical path
        sum += tmp;                // fadd: 1-cycle critical path
    }
    return sum;
}
```

GCC equivalent: `__attribute__((optimize("-ffp-contract=off")))` on the function, or `-ffp-contract=off` for the translation unit.

### Strategy 2: Manually split with a volatile or opaque intermediate

When pragma control is unavailable:

```cpp
float sum_of_squares(const float* a, int n) {
    float sum = 0.0f;
    for (int i = 0; i < n; i++) {
        float tmp = a[i] * a[i];
        asm volatile("" : "+w"(tmp));  // prevent FMA contraction
        sum += tmp;
    }
    return sum;
}
```

### Strategy 3: Combine with multiple accumulators

The best approach is often to both split the FMA and use multiple accumulators, breaking both the latency and the throughput bottleneck:

```cpp
float sum_of_squares(const float* a, int n) {
    float s0 = 0.0f, s1 = 0.0f, s2 = 0.0f, s3 = 0.0f;
    int i = 0;
    #pragma clang fp contract(off)
    for (; i + 3 < n; i += 4) {
        s0 += a[i]   * a[i];
        s1 += a[i+1] * a[i+1];
        s2 += a[i+2] * a[i+2];
        s3 += a[i+3] * a[i+3];
    }
    for (; i < n; i++) s0 += a[i] * a[i];
    return s0 + s1 + s2 + s3;
}
```

## Expected Impact

- **Reduction loops (single accumulator):** disabling FMA contraction can yield up to 4x speedup when the loop is latency-bound on the dependency chain (FMA latency 4 vs ADD latency 1).
- **In practice:** 2-3x speedup is common because other factors (load latency, branch overhead) also contribute to the critical path.
- **Combined with multiple accumulators:** 4-8x speedup over the original FMA-contracted single-accumulator version.
- **Note:** this is the opposite direction from fma-utilization.md, which advises maximizing FMA use. The key distinction is whether the FMA is **on** the critical dependency chain (this pattern: avoid FMA) or **off** the critical path (fma-utilization: use FMA).

## Caveats

- **Only applies to dependency chains:** if the FMA result is not fed back as an input to the next FMA, there is no penalty. FMA in Horner polynomial evaluation, dot products, and matrix multiply are fine as long as multiple accumulators or Estrin's scheme break the chain.
- **FMA is still better for throughput-bound code:** when there is no loop-carried dependency (e.g., element-wise `c[i] = a[i] * b[i] + d[i]`), FMA saves an instruction slot and is always preferred.
- **Numerical precision:** separate mul+add performs two roundings vs one for FMA. Disabling FMA slightly reduces numerical accuracy for the computation. In most cases (reductions, signal processing) this is negligible.
- **Compiler version sensitivity:** different compiler versions and optimization levels may or may not contract mul+add into FMA by default. Always inspect the generated assembly for the target loop.
- **Interaction with vectorization:** SIMD FMA has the same latency trap. `vfmla` (NEON) or `vfmadd` (AVX) on a vector accumulator has the same 4-cycle chain. The fix (split + multiple accumulators) applies equally to scalar and vector code.
