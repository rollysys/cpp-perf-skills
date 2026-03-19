---
name: Manual NEON Intrinsic Idioms
source: ComputeLibrary NEON kernels, perf-ninja core_bound/compiler_intrinsics_1, perf-book Ch.9
layers: [microarchitecture]
platforms: [arm]
keywords: [NEON, intrinsics, vld1, vst1, vmul, vadd, vmla, vfma, float32x4_t, uint8x16_t, arm_neon.h]
---

## Problem

When the compiler fails to auto-vectorize performance-critical loops, or generates suboptimal vector code, manual NEON intrinsics provide direct control over ARM SIMD execution. This is common for:

- Sliding window algorithms where the compiler cannot recognize the optimization opportunity
- Reduction operations requiring specific horizontal operations
- Pooling/convolution kernels with complex access patterns
- Code where auto-vectorization produces unnecessary gather/scatter or masking overhead

## Detection

### When to consider manual NEON intrinsics

1. **Compiler reports vectorization failure** but the algorithm is inherently parallel
2. **Hot loop uses scalar ARM instructions** (`fadd s0, s1, s2`) instead of NEON (`fadd v0.4s, v1.4s, v2.4s`)
3. **The auto-vectorized code has poor throughput** due to excessive shuffles, gathers, or lane insertions
4. **Known algorithmic pattern** exists (e.g., sliding window prefix sums, horizontal reductions) that the compiler does not recognize

### NEON register identification in disassembly

- `v0`-`v31`: 128-bit NEON/FP registers
- `.4s`: 4x 32-bit single-precision floats (`float32x4_t`)
- `.2d`: 2x 64-bit doubles (`float64x2_t`)
- `.16b`: 16x 8-bit bytes (`uint8x16_t`)
- `.8h`: 8x 16-bit half-words (`uint16x8_t`)

## Transformation

### Common NEON types and their sizes

| C type | NEON intrinsic type | Elements | Register portion |
|--------|-------------------|----------|-----------------|
| `float` | `float32x4_t` | 4 | 128-bit (Q) |
| `float` | `float32x2_t` | 2 | 64-bit (D) |
| `uint8_t` | `uint8x16_t` | 16 | 128-bit (Q) |
| `uint8_t` | `uint8x8_t` | 8 | 64-bit (D) |
| `int16_t` | `int16x8_t` | 8 | 128-bit (Q) |
| `uint32_t` | `uint32x4_t` | 4 | 128-bit (Q) |

### Pattern 1: Vectorized load-accumulate-store (from ComputeLibrary pool3d avg pooling)

This pattern from ComputeLibrary's `pool3d/neon/impl.h` shows the canonical NEON loop structure: process vector-width elements per iteration with a scalar tail loop.

```cpp
#include <arm_neon.h>

// Vectorized main loop -- process 4 floats at a time
int x_off = 0;
constexpr int window_step_x = 4; // 128-bit / 32-bit = 4 floats

for (; x_off <= (window_end_x - window_step_x); x_off += window_step_x) {
    // Initialize accumulator to zero
    float32x4_t vres = vdupq_n_f32(0.0f);

    for (int y = 0; y < pool_h; ++y) {
        for (int x = 0; x < pool_w; ++x) {
            const float *in_ptr = base_ptr + y * stride_y + x * stride_x;
            // Load 4 contiguous floats
            float32x4_t data = vld1q_f32(in_ptr + x_off);
            // Accumulate
            vres = vaddq_f32(vres, data);
        }
    }

    // Scale by 1/pool_size
    float32x4_t scale_v = vdupq_n_f32(scale);
    vres = vmulq_f32(vres, scale_v);

    // Store 4 results
    vst1q_f32(out_ptr + x_off, vres);
}

// Scalar tail loop for remainder
for (; x_off < window_end_x; ++x_off) {
    float res = 0.0f;
    for (int y = 0; y < pool_h; ++y) {
        for (int x = 0; x < pool_w; ++x) {
            res += *(base_ptr + y * stride_y + x * stride_x + x_off);
        }
    }
    res *= scale;
    *(out_ptr + x_off) = res;
}
```

Key pattern elements from ComputeLibrary:
- `vdupq_n_f32(val)`: broadcast scalar to all lanes (initialize accumulator)
- `vld1q_f32(ptr)`: load 4 contiguous floats (the `q` suffix means 128-bit)
- `vaddq_f32(a, b)`: element-wise addition
- `vmulq_f32(a, b)`: element-wise multiplication
- `vst1q_f32(ptr, val)`: store 4 contiguous floats
- Scalar tail loop handles `N % 4` remaining elements

### Pattern 2: Fused multiply-accumulate and L2 pooling (from ComputeLibrary)

ComputeLibrary's L2 pooling uses `vmla` (multiply-accumulate) to compute sum-of-squares, then `vinvsqrt`/`vinv` for the square root:

```cpp
// Sum of squares using fused multiply-accumulate
float32x4_t vres = vdupq_n_f32(0.0f);
for (int i = 0; i < count; i += 4) {
    float32x4_t data = vld1q_f32(in_ptr + i);
    // vres += data * data (fused multiply-accumulate)
    vres = vmlaq_f32(vres, data, data);
}

// On ARMv8.2+, prefer vfmaq_f32 for better precision:
// vres = vfmaq_f32(vres, data, data);  // fused multiply-add (single rounding)

// Scale and sqrt
vres = vmulq_f32(vres, vdupq_n_f32(scale));
// Newton-Raphson inverse sqrt: faster than vsqrtq_f32 in some contexts
float32x4_t inv_sqrt = vrsqrteq_f32(vres);       // initial estimate
inv_sqrt = vmulq_f32(vrsqrtsq_f32(vmulq_f32(vres, inv_sqrt), inv_sqrt), inv_sqrt);
vres = vmulq_f32(vres, inv_sqrt);                 // x * (1/sqrt(x)) = sqrt(x)
```

### Pattern 3: Horizontal reduction (from ComputeLibrary reduction_layer impl.h)

Reducing a vector to a single scalar (min/max/sum) requires pairwise operations. This pattern from ComputeLibrary:

```cpp
// Horizontal min of float32x4_t -- from ComputeLibrary calculate_min()
inline float32x2_t horizontal_min(float32x4_t in) {
    // Pairwise min of high and low halves
    float32x2_t pmin = vpmin_f32(vget_high_f32(in), vget_low_f32(in));
    // Pairwise min again to get single minimum in both lanes
    return vpmin_f32(pmin, pmin);
}

// Horizontal max of float32x4_t -- from ComputeLibrary calculate_max()
inline float32x2_t horizontal_max(float32x4_t in) {
    float32x2_t pmax = vpmax_f32(vget_high_f32(in), vget_low_f32(in));
    return vpmax_f32(pmax, pmax);
}

// For uint8x16_t, need 4 levels of pairwise reduction:
inline uint8x8_t horizontal_min_u8(uint8x16_t in) {
    uint8x8_t pmin = vpmin_u8(vget_high_u8(in), vget_low_u8(in));
    pmin = vpmin_u8(pmin, pmin);
    pmin = vpmin_u8(pmin, pmin);
    return vpmin_u8(pmin, pmin);
}

// For AArch64, use the single-instruction variant:
// float result = vminnmvq_f32(in);  // horizontal min across all lanes
```

### Pattern 4: Sliding window with intrinsics (from perf-ninja compiler_intrinsics_1)

The original scalar sliding window for image smoothing:

```cpp
// Scalar version -- compiler struggles to vectorize due to
// overlapping read/write windows and carried sum dependency
for (; pos < limit; ++pos) {
    currentSum -= input[pos - radius - 1];
    currentSum += input[pos + radius];
    output[pos] = currentSum;
}
```

Optimized approach using NEON: split the subtract and add into vectorized prefix-sum operations, or vectorize across independent output positions:

```cpp
// Vectorize by processing 8 output positions in parallel
// Each output[i] = sum(input[i-radius .. i+radius])
// Pre-compute prefix sums, then output[i] = prefix[i+radius+1] - prefix[i-radius]
#include <arm_neon.h>

// After computing prefix sums in 'prefix' array:
int pos = 0;
for (; pos + 4 <= limit; pos += 4) {
    // Load prefix[pos + radius + 1 .. pos + radius + 4]
    uint32x4_t right = vld1q_u32(&prefix[pos + radius + 1]);
    // Load prefix[pos - radius .. pos - radius + 3]  (or 0 for left border)
    uint32x4_t left  = vld1q_u32(&prefix[pos - radius]);
    // output = right - left
    uint32x4_t result = vsubq_u32(right, left);
    vst1q_u32(&output_u32[pos], result);
}
// Scalar remainder
for (; pos < limit; ++pos) {
    output_u32[pos] = prefix[pos + radius + 1] - prefix[pos - radius];
}
```

### Pattern 5: Bitwise select for branchless conditional (from ComputeLibrary)

The `vbsl` (bitwise select) intrinsic implements branchless conditional assignment, critical for avoiding branch mispredictions in SIMD code:

```cpp
// From ComputeLibrary: conditional index update for argmin/argmax
uint32x4_t mask = vcgtq_f32(old_val, new_val);  // mask = (old > new) ? 0xFFFFFFFF : 0
// Select new_idx where mask is set, keep old_idx otherwise
uint32x4_t result_idx = vbslq_u32(mask, new_idx, old_idx);
float32x4_t result_val = vbslq_f32(mask,
    vreinterpretq_f32_u32(vreinterpretq_u32_f32(new_val)),
    vreinterpretq_f32_u32(vreinterpretq_u32_f32(old_val)));
```

## Expected Impact

| Transformation | Typical Speedup |
|---------------|-----------------|
| Scalar loop to NEON vectorized loop (4x float) | 2-4x |
| Scalar loop to NEON vectorized loop (16x uint8) | 4-12x |
| Auto-vectorized to hand-tuned (sliding window) | 1.5-3x |
| Branching conditional to vbsl | 2-5x (when branch misprediction is high) |
| vmulq+vaddq replaced by vfmaq (FMA) | 1.1-1.3x (fewer instructions, single rounding) |

## Caveats

- **Always provide a scalar fallback** for non-ARM platforms. Use `#ifdef __ARM_NEON` or `#if defined(__aarch64__)` guards.
- **Tail handling is your responsibility**: unlike auto-vectorization, intrinsics do not automatically generate remainder loops. Always handle `N % vector_width` remaining elements.
- **AArch32 vs AArch64 differences**: some intrinsics (e.g., `vmaxvq_f32` horizontal max) are only available on AArch64. AArch32 NEON has 16 Q-registers vs 32 on AArch64.
- **Prefer `vfmaq` over separate `vmulq`+`vaddq`** on ARMv8.2+: fused multiply-add has better precision (single rounding) and throughput. However, the result will differ slightly from separate multiply-then-add due to rounding.
- **Register pressure**: NEON has 32 x 128-bit registers on AArch64. Excessive unrolling with many live vector variables will cause register spills. Profile to find the optimal unroll factor.
- **Alignment**: `vld1q`/`vst1q` handle unaligned access on AArch64 without penalty on most modern cores (Cortex-A76+). On older cores (Cortex-A53), aligned access via `vld1q` with naturally aligned pointers can be faster.
- **Do not use intrinsics if auto-vectorization produces equivalent code**: intrinsics are harder to read, maintain, and port. Verify with compiler output (`-S`) that the compiler is not already generating the same instructions.
