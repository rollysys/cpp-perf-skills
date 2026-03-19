---
name: Fused Multiply-Add Utilization
source: optimized-routines math/aarch64/advsimd/, perf-book Ch.9
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [FMA, fused multiply-add, fmla, vfmadd, polynomial, Horner, multiply-accumulate, MAC]
---

## Problem

Many numerical computations contain sequences of multiply-then-add operations: `a = a + b * c`. When these are compiled as separate multiply and add instructions, they consume two instruction slots, two cycles of execution-unit throughput, and introduce an intermediate rounding step. Modern CPUs provide Fused Multiply-Add (FMA) instructions that perform `a + b * c` in a single instruction with:

- **Same latency** as a standalone multiply (~4 cycles on most cores)
- **Half the instruction count** (one FMA replaces one MUL + one ADD)
- **Better numerical accuracy** (single rounding instead of two)

FMA is critical in polynomial evaluation, dot products, matrix multiplication, and math library implementations. Failing to utilize FMA means leaving up to 2x peak FLOPS on the table.

## Detection

**Source-level indicators:**
- Explicit patterns: `result = a + b * c` or `result += b * c`
- Polynomial evaluation not using Horner's method: `c0 + c1*x + c2*x*x + c3*x*x*x`
- Separate multiply and add operations on floating-point values in tight loops
- Manual intrinsics using `vmul` + `vadd` instead of `vfma`

**Assembly-level indicators:**

ARM NEON -- should see FMA instructions:
```asm
; Good: FMA utilized
fmla   v0.4s, v1.4s, v2.4s          ; scalar: fmadd s0, s1, s2, s3
vfmaq_f32(a, b, c)                   ; intrinsic form
vfmaq_laneq_f32(a, b, c, lane)       ; FMA with lane broadcast

; Bad: separate mul + add
fmul   v0.4s, v1.4s, v2.4s
fadd   v3.4s, v3.4s, v0.4s
```

x86 AVX2/AVX-512 -- should see vfmadd instructions:
```asm
; Good: FMA utilized
vfmadd231ps ymm0, ymm1, ymm2     ; ymm0 = ymm1 * ymm2 + ymm0
vfmadd213ps ymm0, ymm1, ymm2     ; ymm0 = ymm1 * ymm0 + ymm2

; Bad: separate mul + add
vmulps ymm3, ymm1, ymm2
vaddps ymm0, ymm0, ymm3
```

**Compiler flags needed:**
- GCC/Clang: `-mfma` (x86), or `-march=armv8-a+fp` (ARM, usually default)
- `-ffp-contract=fast` enables FMA contraction (Clang default is `-ffp-contract=on` which only contracts within a single expression; `fast` contracts across statements)
- `-ffast-math` implies `-ffp-contract=fast`

## Transformation

### Pattern 1: Horner's method for polynomial evaluation

Polynomial evaluation is the canonical FMA use case. The Arm optimized-routines library (expf.c, logf.c) demonstrates this extensively.

**Before** -- naive polynomial (separate mul+add, poor FMA utilization):
```cpp
// p(x) = c0 + c1*x + c2*x^2 + c3*x^3 + c4*x^4
float poly_naive(float x, float c0, float c1, float c2, float c3, float c4) {
    float x2 = x * x;
    float x3 = x2 * x;
    float x4 = x2 * x2;
    return c0 + c1*x + c2*x2 + c3*x3 + c4*x4;
    // 4 multiplies for powers + 4 multiplies for coeff*power + 4 adds = 12 ops
    // Long dependency chain for x^4, but no FMA chaining
}
```

**After** -- Horner's method (natural FMA chain):
```cpp
// p(x) = c0 + x*(c1 + x*(c2 + x*(c3 + x*c4)))
float poly_horner(float x, float c0, float c1, float c2, float c3, float c4) {
    float r = c4;
    r = r * x + c3;   // fmadd
    r = r * x + c2;   // fmadd
    r = r * x + c1;   // fmadd
    r = r * x + c0;   // fmadd
    return r;
    // 4 FMA instructions total, serial dependency chain of length 4
}
```

Horner's method uses N FMA ops for degree-N polynomial (vs ~2N separate ops). The dependency chain length is N * FMA_latency, but for short polynomials (degree 4-7) this is acceptable and produces fewer total instructions.

### Pattern 2: Estrin's scheme for ILP + FMA (from optimized-routines expf.c)

For higher-degree polynomials, Horner creates a long serial chain. The ARM optimized-routines library uses a **pairwise/Estrin** approach to expose parallelism while still using FMA:

```c
// From optimized-routines expf.c -- polynomial evaluation using FMA + parallelism
// Coefficients c0..c4 for exp approximation
float32x4_t r2 = vmulq_f32(r, r);                       // r^2 (independent)
float32x4_t p = vfmaq_laneq_f32(d->c1, r, ln2_c02, 2); // p = c1 + r*c0
float32x4_t q = vfmaq_laneq_f32(d->c3, r, ln2_c02, 3); // q = c3 + r*c2 (parallel with p!)
q = vfmaq_f32(q, p, r2);                                 // q = q + p*r^2
p = vmulq_f32(d->c4, r);                                 // p = c4 * r
float32x4_t poly = vfmaq_f32(p, q, r2);                  // poly = p + q*r^2
```

Key insight: by computing `p` (even terms) and `q` (odd terms) in parallel, then combining with r^2, you get ILP + FMA utilization. The dependency depth is ~3 FMA operations instead of 4 serial ones.

### Pattern 3: logf polynomial with Estrin's (from optimized-routines logf.c)

```c
// From optimized-routines logf.c -- degree-7 polynomial for log
// Parallel evaluation: p, q, y computed on independent chains
float32x4_t r2 = vmulq_f32(r, r);
float32x4_t p = vfmaq_laneq_f32(d->c2, r, c1350, 0);   // p = c2 + r*c1
float32x4_t q = vfmaq_laneq_f32(d->c4, r, c1350, 1);   // q = c4 + r*c3
float32x4_t y = vfmaq_laneq_f32(d->c6, r, c1350, 2);   // y = c6 + r*c5
p = vfmaq_laneq_f32(p, r2, c1350, 3);                    // p = p + r^2*c0

q = vfmaq_f32(q, p, r2);                                 // merge p into q
y = vfmaq_f32(y, q, r2);                                 // merge q into y
p = vfmaq_f32(r, d->ln2, n);                             // p = r + ln2*n
return vfmaq_f32(p, y, r2);                              // result = p + y*r^2
```

Three parallel FMA chains (p, q, y) computed simultaneously, then folded together. Degree-7 polynomial evaluated in ~4 dependent FMA operations instead of 7.

### Pattern 4: Scalar FMA via compiler hints

When you cannot use intrinsics, ensure the compiler contracts mul+add into FMA:

```cpp
// Ensure FMA contraction with pragma (Clang 18+)
#pragma clang fp contract(fast)
float dot_product(const float* a, const float* b, int n) {
    float sum = 0.0f;
    for (int i = 0; i < n; i++) {
        sum += a[i] * b[i];   // compiler will emit fmadd/vfmadd
    }
    return sum;
}
```

Or use the standard C `fma()` function to force FMA even without fast-math:
```cpp
#include <cmath>
sum = std::fma(a[i], b[i], sum);  // guaranteed single FMA instruction on HW with FMA support
```

## Expected Impact

- **Instruction count:** 2x reduction for multiply-accumulate patterns. A degree-4 polynomial goes from ~12 instructions to 4 FMAs.
- **Throughput:** ARM Cortex-A76/X1 can issue 2 FMA per cycle (4-cycle latency). x86 Haswell+ can issue 2 FMA per cycle (5-cycle latency). Theoretical peak doubles vs separate mul+add.
- **Polynomial evaluation:** Estrin's scheme on NEON: degree-7 polynomial in ~4 dependent FMA ops (16 cycles) vs Horner's 7 dependent ops (28 cycles) -- ~1.75x faster. Compared to naive: ~3x faster.
- **Real-world math functions:** ARM optimized-routines achieves 2-5x speedup over system libm for expf/logf/sinf, with FMA as a core enabler.

## Caveats

- **FMA changes numerical results:** `fma(a,b,c)` produces a single-rounded result, which differs slightly from `a*b+c` (double-rounded). This is typically more accurate, but can break exact bit-for-bit reproducibility with non-FMA code paths.
- **Compiler behavior varies:** Clang with default `-ffp-contract=on` only contracts within a single C expression. Across statements, you need `-ffp-contract=fast` or `#pragma clang fp contract(fast)`. GCC defaults to `-ffp-contract=fast`.
- **x86 requires explicit ISA flag:** FMA instructions need `-mfma` or `-march=haswell` (or later). Without this flag, the compiler will never emit FMA even if the pattern is recognized.
- **Horner vs Estrin trade-off:** Horner has the shortest total instruction count but longest serial dependency chain. Estrin uses more multiplications but exposes more ILP. For short polynomials (degree <= 4), Horner may be faster because OOO execution can overlap the chain with surrounding code. For longer polynomials or SIMD code, Estrin wins.
- **std::fma() overhead:** On hardware without FMA support, `std::fma()` falls back to a software emulation that is ~10x slower than separate mul+add. Always confirm HW support.
- **Register pressure with Estrin:** Maintaining multiple parallel chains increases live register count. On ARM NEON (32 vector registers) this is rarely a problem. On x86 with only 16 YMM registers, aggressive Estrin schemes may cause spills.
