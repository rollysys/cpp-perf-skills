---
name: Overlapping Tail Load/Store (Branchless Remainder Handling)
source: optimized-routines string/aarch64/memcpy.S
layers: [microarchitecture]
platforms: [arm]
keywords: [tail handling, remainder, NEON, overlap, branchless, memcpy, vector cleanup, mask]
---

## Problem

The standard approach to handling "remainder" elements after a SIMD main loop is a scalar cleanup loop that processes leftover elements one-by-one. Each element requires a branch (the loop condition), and for short buffers (< vector width), the scalar path dominates total execution time. On ARM Neoverse and Cortex-A series, a mispredicted branch costs 10-15 cycles, and the scalar tail loop has poor IPC because it cannot fill the SIMD pipeline.

ARM's optimized-routines memcpy handles the 0-32 byte case with only 3 branches total, by overlapping loads/stores from the end of the buffer.

```cpp
// Typical scalar tail -- branch per element, terrible IPC
for (int i = vec_count * 16; i < n; ++i) {
    dst[i] = src[i];
}
```

## Detection

- Hot loop has a scalar cleanup after the SIMD main loop
- Profiler shows the tail/remainder code consuming disproportionate time for small inputs
- Disassembly shows a `b.lt` / `b.lo` loop back-edge for individual byte/element processing
- Function handles variable-length buffers where many calls are below 1-2 vector widths (e.g., short strings, small memcpy, packet headers)

## Transformation

### Core idea

Instead of processing remaining elements one at a time, perform a full vector load/store from the END of the buffer, overlapping with the last vector-aligned operation. The overlapping region gets the same values written twice, which is harmless but eliminates all per-element branches.

### Pattern from ARM's memcpy (0-32 byte copies)

ARM's optimized-routines `memcpy.S` handles small copies with size-based dispatch and overlapping tail loads:

```
// ARM assembly from optimized-routines memcpy.S (simplified)
// x0 = dst, x1 = src, x2 = count
// x3 = srcend = src + count
// x4 = dstend = dst + count

// 16-32 bytes: two overlapping 16-byte loads/stores
    ldp  q0, q1, [x1]         // load first 32 bytes from src start
    ldp  q2, q3, [x3, #-32]   // load last 32 bytes from src END (overlaps!)
    stp  q0, q1, [x0]         // store first 32 to dst start
    stp  q2, q3, [x4, #-32]   // store last 32 to dst END (overlaps!)
    ret

// 8-16 bytes: two overlapping 8-byte loads/stores
    ldr  x5, [x1]             // load first 8 bytes
    ldr  x6, [x3, #-8]        // load last 8 bytes from END
    str  x5, [x0]             // store first 8
    str  x6, [x4, #-8]        // store last 8 (overlaps for count 9-15)
    ret

// 4-8 bytes: two overlapping 4-byte loads/stores
    ldr  w5, [x1]
    ldr  w6, [x3, #-4]
    str  w5, [x0]
    str  w6, [x4, #-4]
    ret
```

### C/intrinsics equivalent

```cpp
#include <arm_neon.h>
#include <cstring>

// Overlapping-tail memcpy for 0-32 bytes
void small_memcpy(uint8_t* dst, const uint8_t* src, size_t n) {
    if (n >= 16) {
        // 16-32 bytes: load from start AND end, store to start AND end
        uint8x16_t head = vld1q_u8(src);
        uint8x16_t tail = vld1q_u8(src + n - 16);  // overlaps with head!
        vst1q_u8(dst, head);
        vst1q_u8(dst + n - 16, tail);               // overlaps with head store!
    } else if (n >= 8) {
        // 8-15 bytes: same idea with 8-byte loads
        uint64_t head, tail;
        memcpy(&head, src, 8);
        memcpy(&tail, src + n - 8, 8);
        memcpy(dst, &head, 8);
        memcpy(dst + n - 8, &tail, 8);
    } else if (n >= 4) {
        // 4-7 bytes
        uint32_t head, tail;
        memcpy(&head, src, 4);
        memcpy(&tail, src + n - 4, 4);
        memcpy(dst, &head, 4);
        memcpy(dst + n - 4, &tail, 4);
    } else if (n > 0) {
        // 1-3 bytes: head + middle + tail byte trick
        dst[0] = src[0];
        dst[n / 2] = src[n / 2];
        dst[n - 1] = src[n - 1];
    }
}
```

### Applying to generic SIMD loops

```cpp
// Before: standard SIMD loop + scalar tail
void vec_add(float* dst, const float* a, const float* b, int n) {
    int i = 0;
    for (; i + 4 <= n; i += 4) {
        float32x4_t va = vld1q_f32(a + i);
        float32x4_t vb = vld1q_f32(b + i);
        vst1q_f32(dst + i, vaddq_f32(va, vb));
    }
    // Scalar tail -- up to 3 branches
    for (; i < n; ++i) {
        dst[i] = a[i] + b[i];
    }
}

// After: overlapping tail -- 0 or 1 branch for tail
void vec_add(float* dst, const float* a, const float* b, int n) {
    int i = 0;
    for (; i + 4 <= n; i += 4) {
        float32x4_t va = vld1q_f32(a + i);
        float32x4_t vb = vld1q_f32(b + i);
        vst1q_f32(dst + i, vaddq_f32(va, vb));
    }
    // Overlapping tail: re-process last 4 elements from the end
    if (i < n) {
        float32x4_t va = vld1q_f32(a + n - 4);
        float32x4_t vb = vld1q_f32(b + n - 4);
        vst1q_f32(dst + n - 4, vaddq_f32(va, vb));
    }
}
```

### Why the overlap is safe

The overlapping region receives the same computation twice. For idempotent operations (copy, add, min, max, bitwise ops), writing the same value twice is harmless. For non-idempotent operations (e.g., XOR, increment), the overlap technique cannot be used directly.

## Expected Impact

| Scenario | Branches eliminated | Typical speedup |
|----------|-------------------|-----------------|
| memcpy < 32 bytes | All tail branches (up to 31) | 3-5x vs byte loop |
| memcpy 17-31 bytes | All | 2-3x vs byte loop |
| SIMD loop with 1-3 element remainder | 1-3 branches | 1.2-1.5x overall |
| Short-buffer-dominated workload (avg < 64B) | Majority of runtime | 2-4x |

ARM's optimized-routines memcpy achieves 0 branches for any copy <= 128 bytes (using a cascade of overlapping load/store pairs at decreasing widths).

## Caveats

- **Only works for idempotent operations.** If the operation is not safe to repeat (e.g., `dst[i] ^= src[i]`, `dst[i]++`), the overlapping region will produce wrong results. For such cases, use masked stores (`vst1q_lane` or SVE predicated stores) instead.
- **Requires n >= vector_width for the first vector op.** If n can be 0, guard the entire function. If n < vector_width, fall through to the smaller-width overlap cases.
- **Source and destination must not partially overlap.** If `src` and `dst` overlap in a way where the tail store corrupts unread source bytes, the result is incorrect. ARM's memcpy handles this by dispatching to a backward-copy variant.
- **Read beyond allocation.** The tail load reads from `[end - 16]` which may extend before the start of the logical buffer. Ensure the allocation has at least vector_width bytes, or guard with a size check.
