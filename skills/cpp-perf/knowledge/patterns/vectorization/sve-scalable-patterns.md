---
name: SVE Scalable Vector Patterns
source: optimized-routines math/aarch64/sve/, string/aarch64/memcpy-sve.S
layers: [microarchitecture]
platforms: [arm]
keywords: [SVE, scalable, predicate, whilelt, svfloat32_t, vector length agnostic, sve2]
---

## Problem

ARM SVE (Scalable Vector Extension) enables vector-length-agnostic (VLA) programming where the same binary runs efficiently on hardware with different vector widths (128-bit to 2048-bit). Unlike NEON's fixed 128-bit vectors, SVE code adapts automatically to the hardware's vector length. This is critical for:

- Writing portable high-performance code across ARM hardware generations
- Eliminating scalar remainder loops through predicated operations
- Leveraging wider vectors on server-class ARM processors (e.g., Neoverse V1: 256-bit, Neoverse V2: 128-bit SVE2)

## Detection

### When SVE is beneficial over NEON

1. **Target hardware has SVE with vectors wider than 128-bit**: SVE on Neoverse V1 (256-bit) processes 2x the elements per instruction vs NEON
2. **Loop has complex remainder handling**: SVE predicates eliminate tail loops entirely
3. **Code needs to run across multiple ARM generations**: VLA model means one binary works on 128-bit through 2048-bit implementations
4. **Small copy/memset operations**: SVE `whilelt`-based loops handle arbitrary sizes without branching (as in optimized-routines memcpy-sve.S)

### SVE instruction identification in disassembly

- `z0`-`z31`: scalable vector registers (width determined by hardware)
- `p0`-`p15`: predicate registers (one bit per byte lane)
- `whilelt`, `whilelo`: generate predicates for loop control
- `ld1w`, `ld1b`, `st1w`, `st1b`: predicated load/store
- `fadd`, `fmul`, `fmla` with `z` registers: SVE arithmetic
- `svptrue_b32()`, `svptrue_b8()`: all-true predicates

## Transformation

### Pattern 1: SVE whilelt loop pattern (from optimized-routines memcpy-sve.S)

The most distinctive SVE idiom is the `whilelt`-based loop that replaces both the main loop and the remainder loop. From `memcpy-sve.S`, small copies (up to 2x vector length) use just two predicated load/stores with no branching:

```asm
; From optimized-routines/string/aarch64/memcpy-sve.S
; Small copies: up to 2 * VL bytes, no branch needed

    cntb    vlen                      ; vlen = vector length in bytes
    whilelo p0.b, xzr, count         ; p0 = predicate for [0, count)
    whilelo p1.b, vlen, count        ; p1 = predicate for [vlen, count)
    ld1b    z0.b, p0/z, [src, #0, mul vl]   ; load first VL bytes (masked)
    ld1b    z1.b, p1/z, [src, #1, mul vl]   ; load second VL bytes (masked)
    st1b    z0.b, p0, [dst, #0, mul vl]     ; store first VL bytes (masked)
    st1b    z1.b, p1, [dst, #1, mul vl]     ; store second VL bytes (masked)
    ret
```

The equivalent C intrinsics pattern:

```c
#include <arm_sve.h>

void sve_memcpy_small(uint8_t *dst, const uint8_t *src, size_t count) {
    uint64_t vlen = svcntb();  // vector length in bytes
    // First vector: predicate covers [0, count)
    svbool_t p0 = svwhilelt_b8(0UL, count);
    // Second vector: predicate covers [vlen, count)
    svbool_t p1 = svwhilelt_b8(vlen, count);

    svuint8_t z0 = svld1(p0, src);
    svuint8_t z1 = svld1(p1, src + vlen);
    svst1(p0, dst, z0);
    svst1(p1, dst + vlen, z1);
}
```

Key insight: `whilelt` generates a predicate where lanes `[start, end)` are active. If `count < vlen`, `p1` will be all-false and the second load/store becomes a no-op. No branching needed for remainder handling.

### Pattern 2: SVE math kernel -- expf (from optimized-routines)

The SVE `expf` from `optimized-routines/math/aarch64/sve/expf.c` demonstrates SVE-specific features compared to its NEON counterpart.

**SVE version** (key differences highlighted):

```c
// From optimized-routines sve/expf.c
svfloat32_t SV_NAME_F1(exp)(svfloat32_t x, const svbool_t pg) {
    const struct data *d = ptr_barrier(&data);

    // SVE-specific: predicated absolute compare
    svbool_t special = svacgt(pg, x, d->special_bound);

    if (unlikely(svptest_any(special, special)))
        return special_case(x, pg, special, d);

    return expf_inline(x, svptrue_b32(), d);
}

// SVE expf_inline uses:
// - svmad_x: predicated fused multiply-add (a*b + c)
// - svsub_x: predicated subtract
// - svmls_lane: multiply-subtract using lane from another vector
// - svexpa: SVE-specific 2^x approximation instruction
// - svmul_x, svmla_x: predicated multiply, multiply-accumulate
static inline svfloat32_t
expf_inline(svfloat32_t x, const svbool_t pg, const struct data *d) {
    svfloat32_t lane_constants = svld1rq(svptrue_b32(), &d->ln2_hi);

    svfloat32_t z = svmad_x(pg, sv_f32(d->inv_ln2), x, d->shift);
    svfloat32_t n = svsub_x(pg, z, d->shift);

    svfloat32_t r = x;
    r = svmls_lane(r, n, lane_constants, 0);  // r -= n * ln2_hi
    r = svmls_lane(r, n, lane_constants, 1);  // r -= n * ln2_lo

    svfloat32_t scale = svexpa(svreinterpret_u32(z));  // SVE-only: fast 2^n

    svfloat32_t r2 = svmul_x(svptrue_b32(), r, r);
    svfloat32_t poly = svmla_lane(r, r2, lane_constants, 2);

    return svmla_x(pg, scale, scale, poly);  // scale + scale * poly
}
```

**Equivalent NEON version** (from `advsimd/expf.c`) for comparison:

```c
// From optimized-routines advsimd/expf.c
float32x4_t V_NAME_F1(exp)(float32x4_t x) {
    // NEON: no predicate argument, fixed 4-lane width
    float32x4_t n = vrndaq_f32(vmulq_f32(x, d->inv_ln2));
    float32x4_t r = vfmsq_laneq_f32(x, n, ln2_c02, 0);
    r = vfmsq_laneq_f32(r, n, ln2_c02, 1);

    // NEON: manual bit manipulation for 2^n (no svexpa equivalent)
    uint32x4_t e = vshlq_n_u32(
        vreinterpretq_u32_s32(vcvtq_s32_f32(n)), 23);
    float32x4_t scale = vreinterpretq_f32_u32(
        vaddq_u32(e, d->exponent_bias));

    // NEON: same polynomial, different intrinsic names
    float32x4_t r2 = vmulq_f32(r, r);
    float32x4_t p = vfmaq_laneq_f32(d->c1, r, ln2_c02, 2);
    float32x4_t q = vfmaq_laneq_f32(d->c3, r, ln2_c02, 3);
    q = vfmaq_f32(q, p, r2);
    p = vmulq_f32(d->c4, r);
    float32x4_t poly = vfmaq_f32(p, q, r2);

    return vfmaq_f32(scale, poly, scale);
}
```

Key SVE advantages visible in this comparison:

| Feature | SVE | NEON |
|---------|-----|------|
| Vector width | Scalable (runtime) | Fixed 128-bit |
| Predication | First-class (`pg` argument) | Manual masking with `vbsl` |
| 2^n computation | `svexpa` (single instruction) | Manual shift+add (3 instructions) |
| Function signature | `(svfloat32_t x, svbool_t pg)` | `(float32x4_t x)` |
| Inactive lane control | Predicate masks | Not available (always all lanes) |

### Pattern 3: SVE logf with predicated special-case handling (from optimized-routines)

The SVE `logf` from `sve/logf.c` shows how predicates enable efficient special-case handling:

```c
// From optimized-routines sve/logf.c
svfloat32_t SV_NAME_F1(log)(svfloat32_t x, const svbool_t pg) {
    const struct data *d = ptr_barrier(&data);

    svuint32_t u_off = svreinterpret_u32(x);
    u_off = svsub_x(pg, u_off, d->off);

    // Detect special cases (subnormal, negative, inf, nan)
    svbool_t special = svcmpge(pg,
        svsub_x(pg, u_off, d->lower), d->thresh);

    if (unlikely(svptest_any(special, special)))
        return special_case(x, pg, special, d);

    return v_logf_inline(u_off, pg, d);
}
```

The special case handler uses `svsel` for predicated selection (equivalent to NEON's `vbsl` but integrated with the predicate system):

```c
// SVE predicated special-case handling
svfloat32_t special_log = svsel(is_sub, sv_f32(d->ln2_23), sv_f32(NAN));
special_log = svsel(is_minf, sv_f32(-INFINITY), special_log);
special_log = svsel(is_pinf, sv_f32(INFINITY), special_log);

// Only modify special lanes in the result
return svadd_m(special, ret_log, special_log);
```

Compare with the NEON version which uses bitwise-select chains:

```c
// NEON: manual masking for special cases (from advsimd/logf.c)
y = vbslq_f32(infnan_or_zero, d->nan, y);
y = vbslq_f32(ret_pinf, d->pinf, y);
y = vbslq_f32(ret_minf, d->minf, y);
```

### Pattern 4: SVE constant loading with svld1rq

SVE provides `svld1rq` to load a 128-bit constant and replicate it across the full vector width, used extensively for polynomial coefficients:

```c
// Load 4 floats and replicate across full SVE vector width
// Allows using svmla_lane / svmls_lane to pick individual coefficients
svfloat32_t lane_constants = svld1rq(svptrue_b32(), &d->ln2_hi);

// Use individual lanes as scalar multipliers:
r = svmls_lane(r, n, lane_constants, 0);  // r -= n * lane_constants[0]
r = svmls_lane(r, n, lane_constants, 1);  // r -= n * lane_constants[1]
```

This packs 4 constants into one register and avoids separate scalar loads, reducing register pressure for polynomial evaluation.

## Expected Impact

| Scenario | Speedup vs NEON |
|----------|----------------|
| Hardware with 256-bit SVE (Neoverse V1) | ~2x throughput |
| Hardware with 128-bit SVE2 (Neoverse V2) | ~1.0-1.1x (same width, but no remainder loop) |
| Small variable-size copies (memcpy < 2*VL) | 1.3-2x (branchless via whilelt) |
| Math kernels with special cases (exp, log) | 1.1-1.5x (svexpa, predication overhead savings) |
| Loop with complex remainder | 1.05-1.2x on 128-bit SVE (eliminates tail code) |

## Caveats

- **SVE is not universally available on ARM**: SVE requires ARMv8.2+ with SVE extension. Many mobile SoCs (Cortex-A78, Apple Silicon) do not implement SVE. Check with `__ARM_FEATURE_SVE` at compile time or `/proc/cpuinfo` at runtime.
- **SVE at 128-bit width is not faster than NEON for raw compute**: on Neoverse V2 (128-bit SVE2), SVE instructions process the same number of elements per cycle as NEON. The benefit comes only from VLA portability and predication eliminating remainder loops.
- **Predication has overhead**: `_m` (merging) and `_z` (zeroing) predicate variants have different costs. `_x` (don't care) is fastest when inactive lane values do not matter.
- **VLA types cannot be used in structs/classes, std::vector, or as return values in C++**: `svfloat32_t` is a sizeless type. This affects API design significantly.
- **Compiler support required**: SVE intrinsics need GCC 10+ or Clang 12+ with `-march=armv8.2-a+sve`. MSVC does not support SVE intrinsics.
- **Auto-vectorization to SVE is improving rapidly**: before writing manual SVE intrinsics, check if `-march=armv8.2-a+sve -O3` produces acceptable code. Compilers can often generate good SVE from the same source that they vectorize to NEON.
- **SVE2 vs SVE**: SVE2 (ARMv9) adds instructions for per-lane shifts, complex multiply, cryptography, and narrowing operations. Code targeting both should check `__ARM_FEATURE_SVE2`.
