---
name: NEON Pairwise Reduction Cascade
source: optimized-routines string/aarch64/strlen.S, networking/chksum_simd.c
layers: [microarchitecture]
platforms: [arm]
keywords: [horizontal reduction, pairwise, uminp, addp, vpadalq, accumulate, NEON reduce]
---

## Problem

After SIMD processing (e.g., comparing 16 or 32 bytes for a NUL terminator, accumulating checksums), you need to reduce a vector to a scalar result: "which byte matched?", "what is the total sum?", "is any element nonzero?". A naive approach extracts each lane individually and checks in scalar code, wasting cycles on lane-crossing operations and branches.

ARM's optimized-routines uses pairwise reduction instructions (`uminp`, `addp`, `vpadalq`) to collapse vectors in O(log N) steps, then a single `fmov` to move the result to a general-purpose register for branching.

## Detection

- SIMD code followed by scalar extraction of individual lanes (`vgetq_lane_u8`, `vget_lane_u32`, etc.) in a loop
- Horizontal reduction (sum, min, max, OR, AND) needed after SIMD comparison or accumulation
- Checksum/hash accumulation loops that need to merge partial results from multiple NEON accumulators
- `strlen`-like patterns where you need to detect the first zero byte across a 16B or 32B vector

## Transformation

### Pattern 1: NUL byte detection in strlen (from optimized-routines strlen.S)

ARM's strlen loads 32 bytes (2 x 16B registers), uses `uminp` to cascade-reduce and detect any zero byte, then branches on the scalar result:

```
// Load 32 bytes (2 vectors)
ldp  q0, q1, [x0]                // q0 = bytes[0..15], q1 = bytes[16..31]

// Pairwise minimum: reduces 32B -> 16B
// uminp takes pairwise min of adjacent elements
uminp v0.16b, v0.16b, v1.16b     // v0 = pairwise_min(q0, q1), 32 bytes -> 16 bytes

// Reduce 16B -> 8B
uminp v0.16b, v0.16b, v0.16b     // 16 bytes -> 8 bytes (in low 64 bits)

// Compare to zero
cmeq  v0.8b, v0.8b, #0           // each byte: 0xFF if zero found, 0x00 otherwise

// Move to scalar for branching
fmov  x0, d0                     // move low 64 bits to x0

// If x0 != 0, a NUL was found somewhere in the 32 bytes
cbnz  x0, .Lfound
```

**Why pairwise min works:** `uminp` takes the minimum of adjacent byte pairs. If ANY byte in the original 32B is zero, the minimum propagates through the cascade and the final comparison detects it. This checks 32 bytes for NUL in just 4 instructions before the branch.

### Pattern 2: Checksum accumulation with vpadalq (from optimized-routines chksum_simd.c)

Internet checksum requires summing 16-bit values without overflow. `vpadalq` (pairwise add and accumulate long) widens elements while adding, preventing overflow:

```cpp
#include <arm_neon.h>

// Accumulate 16-bit values into 32-bit accumulators without overflow
// vpadalq_u32: pairwise-add adjacent u16 elements, accumulate into u32

uint32x4_t vsum = vdupq_n_u32(0);

for (int i = 0; i < n; i += 16) {
    // Load 16 bytes as u8
    uint8x16_t data = vld1q_u8(buf + i);

    // Widen u8 -> u16 (low and high halves)
    uint16x8_t lo = vmovl_u8(vget_low_u8(data));
    uint16x8_t hi = vmovl_u8(vget_high_u8(data));

    // Pairwise-add u16 pairs into u32, accumulate
    // This adds adjacent 16-bit values and accumulates into 32-bit lanes
    // preventing overflow that would occur with plain u16 addition
    vsum = vpadalq_u32(vsum, vmull_u16(vget_low_u16(lo), vdup_n_u16(1)));
}

// Final horizontal sum: 4 x u32 -> scalar
uint32x2_t pair = vadd_u32(vget_low_u32(vsum), vget_high_u32(vsum));
pair = vpadd_u32(pair, pair);
uint32_t total = vget_lane_u32(pair, 0);
```

### Pattern 3: Generic horizontal reduction template

```cpp
#include <arm_neon.h>

// Horizontal sum of float32x4_t -- 3 instructions
inline float hsum_f32(float32x4_t v) {
    float32x2_t lo = vget_low_f32(v);
    float32x2_t hi = vget_high_f32(v);
    float32x2_t sum = vpadd_f32(lo, hi);    // [a+b, c+d]
    sum = vpadd_f32(sum, sum);               // [a+b+c+d, a+b+c+d]
    return vget_lane_f32(sum, 0);
}

// Horizontal max of uint8x16_t -- 4 pairwise reductions
inline uint8_t hmax_u8(uint8x16_t v) {
    uint8x8_t r = vpmax_u8(vget_low_u8(v), vget_high_u8(v));  // 16 -> 8
    r = vpmax_u8(r, r);                                         // 8 -> 4
    r = vpmax_u8(r, r);                                         // 4 -> 2
    r = vpmax_u8(r, r);                                         // 2 -> 1
    return vget_lane_u8(r, 0);
}

// AArch64 single-instruction alternatives (when available):
// float  result = vmaxvq_f32(v);   // horizontal max across float32x4_t
// float  result = vminvq_f32(v);   // horizontal min
// float  result = vaddvq_f32(v);   // horizontal sum (ARMv8.2+)
// uint8_t result = vmaxvq_u8(v);   // horizontal max across uint8x16_t
// uint8_t result = vminvq_u8(v);   // horizontal min across uint8x16_t
```

### Pattern 4: Combine comparison results from multiple vectors

```cpp
// Check if ANY byte in 64 bytes is zero (4 x 16B vectors)
uint8x16_t v0 = vld1q_u8(ptr);
uint8x16_t v1 = vld1q_u8(ptr + 16);
uint8x16_t v2 = vld1q_u8(ptr + 32);
uint8x16_t v3 = vld1q_u8(ptr + 48);

// Cascade reduction: 64B -> 32B -> 16B -> 8B -> scalar
uint8x16_t min01 = vminq_u8(v0, v1);         // 32B -> 16B
uint8x16_t min23 = vminq_u8(v2, v3);
uint8x16_t min0123 = vminq_u8(min01, min23);  // 16B -> 16B

// Final pairwise reduction to scalar
uint8x8_t r = vpmin_u8(vget_low_u8(min0123), vget_high_u8(min0123));
r = vpmin_u8(r, r);
r = vpmin_u8(r, r);
r = vpmin_u8(r, r);
uint8_t global_min = vget_lane_u8(r, 0);

if (global_min == 0) {
    // NUL found somewhere in 64 bytes -- narrow down which vector
}
```

## Expected Impact

| Operation | Naive (lane extract loop) | Pairwise cascade | Speedup |
|-----------|--------------------------|------------------|---------|
| NUL detection in 32B | 32 lane extracts + 32 compares | 4 instructions | ~8x |
| Horizontal sum float32x4_t | 4 lane extracts + 3 adds | 3 instructions (vpadd) | ~2x |
| Horizontal min uint8x16_t | 16 lane extracts + 15 compares | 4 vpmin instructions | ~4x |
| 64B NUL scan | 64 byte-by-byte checks | 7 NEON instructions | ~10x |
| Checksum 1KB block | scalar add loop | vpadalq cascade | 4-8x |

## Caveats

- **Pairwise operations have higher latency than element-wise ops.** On Cortex-A76, `addp` has 3-cycle latency vs 1-cycle for `add`. The throughput advantage compensates only when reducing many elements.
- **AArch64 has across-lane instructions** (`vmaxvq`, `vminvq`, `vaddvq`) that reduce to scalar in a single instruction. Prefer these when available (ARMv8.0+ for integer, ARMv8.2+ for `vaddvq_f32`). They have 3-4 cycle latency but single-instruction simplicity.
- **The reduction result stays in a NEON register until `fmov`/`vget_lane`.** The NEON-to-GPR transfer (`fmov x0, d0`) has 2-5 cycle latency depending on the core. Avoid frequent NEON-to-scalar round trips in inner loops.
- **For sum reductions with many accumulators**, keep partial sums in vector registers and merge only at the end. See the multi-accumulator-checksum pattern for ILP-maximizing techniques.
