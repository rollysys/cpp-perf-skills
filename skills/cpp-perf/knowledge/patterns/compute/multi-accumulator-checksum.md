---
name: Multi-Accumulator Reduction (ILP-Maximized Checksum)
source: optimized-routines networking/aarch64/chksum_simd.c
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [multi accumulator, ILP, pipeline, checksum, vpadalq, unroll, reduction, latency hiding]
---

## Problem

SIMD reduction loops (checksum, dot product, histogram accumulation) often have a single accumulator that creates a loop-carried dependency chain. Each iteration must wait for the previous accumulation to complete before starting the next one. On modern ARM cores (Neoverse V1/V2, Cortex-X series), NEON add/multiply instructions have 2-4 cycle latency but the pipeline can issue 2-4 SIMD operations per cycle. A single accumulator wastes 50-75% of available SIMD throughput.

ARM's optimized-routines internet checksum implementation uses 4 independent accumulators, each feeding a different pipeline slot. The 4 accumulation chains execute in parallel, hiding the per-instruction latency. Final merge happens once at the end.

```cpp
// Single accumulator -- bottlenecked by 3-cycle vpadalq latency
// Throughput: 1 iteration per 3 cycles (latency-bound)
uint32x4_t vsum = vdupq_n_u32(0);
for (int i = 0; i < n; i += 16) {
    uint8x16_t data = vld1q_u8(buf + i);
    vsum = vpadalq_u16(vsum, vpaddlq_u8(data));  // depends on previous vsum!
}
```

## Detection

- SIMD loop with a single accumulation variable updated every iteration
- Profiler shows low NEON utilization (< 25-50% of peak throughput) despite the loop being compute-bound
- TMA / PMU counters show "backend stall" or "pipeline empty" in a SIMD-heavy loop
- Loop body has 1-2 SIMD instructions but the loop runs at 1 iteration per 3-4 cycles instead of 1 per cycle
- `perf stat` shows IPC < 2 for a loop that should saturate the SIMD pipeline

## Transformation

### Pattern from ARM's chksum_simd.c (4-way accumulator)

```cpp
#include <arm_neon.h>

// Internet checksum: sum of all 16-bit words (ones' complement)
// 4 independent accumulators for 4x ILP
uint32_t checksum_neon(const uint8_t* buf, size_t len) {
    // 4 independent 64-bit accumulator pairs
    uint64x2_t vsum0 = vdupq_n_u64(0);
    uint64x2_t vsum1 = vdupq_n_u64(0);
    uint64x2_t vsum2 = vdupq_n_u64(0);
    uint64x2_t vsum3 = vdupq_n_u64(0);

    size_t i = 0;

    // Main loop: process 64 bytes per iteration (4 x 16B)
    for (; i + 64 <= len; i += 64) {
        // Load 4 x 16B vectors
        uint16x8_t d0 = vreinterpretq_u16_u8(vld1q_u8(buf + i));
        uint16x8_t d1 = vreinterpretq_u16_u8(vld1q_u8(buf + i + 16));
        uint16x8_t d2 = vreinterpretq_u16_u8(vld1q_u8(buf + i + 32));
        uint16x8_t d3 = vreinterpretq_u16_u8(vld1q_u8(buf + i + 48));

        // Each accumulator operates independently -- 4-way ILP
        // vpadalq_u32: pairwise add adjacent u16 elements, accumulate into u32
        // vpadalq_u64: pairwise add adjacent u32 elements, accumulate into u64
        vsum0 = vpadalq_u32(vsum0, vpaddlq_u16(d0));  // chain 0: d0 -> vsum0
        vsum1 = vpadalq_u32(vsum1, vpaddlq_u16(d1));  // chain 1: d1 -> vsum1
        vsum2 = vpadalq_u32(vsum2, vpaddlq_u16(d2));  // chain 2: d2 -> vsum2
        vsum3 = vpadalq_u32(vsum3, vpaddlq_u16(d3));  // chain 3: d3 -> vsum3
        // All 4 vpadalq execute in PARALLEL because they have no data dependency
    }

    // Merge accumulators at the end (once, not per-iteration)
    uint64x2_t vsum = vaddq_u64(vaddq_u64(vsum0, vsum1),
                                 vaddq_u64(vsum2, vsum3));

    // Horizontal reduction: 2 x u64 -> scalar
    uint64_t sum = vgetq_lane_u64(vsum, 0) + vgetq_lane_u64(vsum, 1);

    // Handle remaining bytes with scalar code
    for (; i < len; i += 2) {
        uint16_t word;
        __builtin_memcpy(&word, buf + i, 2);
        sum += word;
    }

    // Fold 64-bit sum to 16-bit ones' complement
    while (sum > 0xFFFF) {
        sum = (sum & 0xFFFF) + (sum >> 16);
    }

    return (uint32_t)sum;
}
```

### Why 4 accumulators? Pipeline analysis

```
Cortex-A76 / Neoverse N1 NEON pipeline:
  vpadalq latency:  3 cycles
  vpadalq throughput: 1 per cycle (on NEON pipe V)

Single accumulator (1-way):
  Cycle 0: vpadalq vsum, data0     -> result ready cycle 3
  Cycle 3: vpadalq vsum, data1     -> result ready cycle 6  (3-cycle gap!)
  Cycle 6: vpadalq vsum, data2     -> result ready cycle 9
  Throughput: 1 iteration per 3 cycles = 33% utilization

4-way accumulator:
  Cycle 0: vpadalq vsum0, data0    -> ready cycle 3
  Cycle 1: vpadalq vsum1, data1    -> ready cycle 4  (no dependency on vsum0!)
  Cycle 2: vpadalq vsum2, data2    -> ready cycle 5
  Cycle 3: vpadalq vsum3, data3    -> ready cycle 6
  Cycle 3: vpadalq vsum0, data4    -> ready cycle 6  (vsum0 from cycle 0 is ready!)
  Throughput: 1 iteration per cycle = 100% utilization
```

The number of accumulators needed = instruction latency / throughput. For 3-cycle latency with 1/cycle throughput, 3 accumulators suffice. 4 is used to provide margin for load latency and other pipeline hazards.

### Generic multi-accumulator template

```cpp
// Generic pattern: N-way accumulation for any reduction operation
template<int N_ACCUM>
float dot_product_multi(const float* a, const float* b, int n) {
    float32x4_t sums[N_ACCUM];
    for (int k = 0; k < N_ACCUM; ++k)
        sums[k] = vdupq_n_f32(0.0f);

    int i = 0;
    constexpr int STRIDE = N_ACCUM * 4;  // N_ACCUM vectors x 4 floats

    for (; i + STRIDE <= n; i += STRIDE) {
        for (int k = 0; k < N_ACCUM; ++k) {
            float32x4_t va = vld1q_f32(a + i + k * 4);
            float32x4_t vb = vld1q_f32(b + i + k * 4);
            sums[k] = vfmaq_f32(sums[k], va, vb);  // independent chain k
        }
    }

    // Merge: tree reduction of accumulators
    float32x4_t total = sums[0];
    for (int k = 1; k < N_ACCUM; ++k)
        total = vaddq_f32(total, sums[k]);

    // Horizontal sum
    float32x2_t r = vadd_f32(vget_low_f32(total), vget_high_f32(total));
    r = vpadd_f32(r, r);
    float result = vget_lane_f32(r, 0);

    // Scalar tail
    for (; i < n; ++i)
        result += a[i] * b[i];

    return result;
}

// Typical usage: 4 accumulators for 3-4 cycle FMA latency
float result = dot_product_multi<4>(a, b, n);
```

### x86 equivalent

The same principle applies to x86 AVX2/AVX-512:

```cpp
// x86 AVX2: 4-way accumulator for dot product
// vfmadd231ps latency: 4 cycles (Skylake), throughput: 0.5 cycles (2 FMA units)
// Need 4/0.5 = 8 accumulators to fully utilize both FMA pipes!
__m256 sum0 = _mm256_setzero_ps();
__m256 sum1 = _mm256_setzero_ps();
__m256 sum2 = _mm256_setzero_ps();
__m256 sum3 = _mm256_setzero_ps();
__m256 sum4 = _mm256_setzero_ps();
__m256 sum5 = _mm256_setzero_ps();
__m256 sum6 = _mm256_setzero_ps();
__m256 sum7 = _mm256_setzero_ps();  // 8 accumulators for 2 FMA ports

for (int i = 0; i + 64 <= n; i += 64) {
    sum0 = _mm256_fmadd_ps(_mm256_loadu_ps(a+i),    _mm256_loadu_ps(b+i),    sum0);
    sum1 = _mm256_fmadd_ps(_mm256_loadu_ps(a+i+8),  _mm256_loadu_ps(b+i+8),  sum1);
    sum2 = _mm256_fmadd_ps(_mm256_loadu_ps(a+i+16), _mm256_loadu_ps(b+i+16), sum2);
    sum3 = _mm256_fmadd_ps(_mm256_loadu_ps(a+i+24), _mm256_loadu_ps(b+i+24), sum3);
    sum4 = _mm256_fmadd_ps(_mm256_loadu_ps(a+i+32), _mm256_loadu_ps(b+i+32), sum4);
    sum5 = _mm256_fmadd_ps(_mm256_loadu_ps(a+i+40), _mm256_loadu_ps(b+i+40), sum5);
    sum6 = _mm256_fmadd_ps(_mm256_loadu_ps(a+i+48), _mm256_loadu_ps(b+i+48), sum6);
    sum7 = _mm256_fmadd_ps(_mm256_loadu_ps(a+i+56), _mm256_loadu_ps(b+i+56), sum7);
}
// Merge 8 accumulators at end
```

## Expected Impact

| Accumulators | Pipeline utilization | Speedup vs 1-accumulator |
|-------------|---------------------|--------------------------|
| 1 (baseline) | 25-33% (latency-bound) | 1x |
| 2 | 50-67% | 1.5-2x |
| 4 | ~100% (ARM NEON) | 3-4x |
| 8 | ~100% (x86 dual FMA) | 3-4x (over 1-accum AVX2) |

ARM's optimized-routines checksum achieves near-peak NEON throughput: 64 bytes processed per ~4 cycles on Neoverse N1, vs ~16 bytes per 4 cycles with a single accumulator.

## Caveats

- **Too many accumulators cause register spills.** AArch64 has 32 NEON registers. With 4 accumulators (4 regs) + 4 data loads (4 regs) + constants (1-2 regs), you use ~10 registers per iteration. Going to 8 accumulators uses ~18 registers -- still fine. Going to 16 would spill and hurt performance. On x86 with AVX2 (16 YMM regs), 8 accumulators + 8 data values = 16 regs, exactly at the limit.
- **The optimal number depends on the specific instruction's latency and throughput on the target core.** Measure, do not guess. Cortex-A55 (in-order) has different characteristics than Cortex-X4 (wide OoO).
- **Floating-point associativity.** Multi-accumulator changes the order of additions, which changes the result for floating-point. The difference is typically within a few ULPs but may affect reproducibility. For integer operations (checksum), this is not an issue.
- **vpadalq (pairwise add and accumulate long) specifically prevents overflow** by widening: it adds adjacent 16-bit values into 32-bit, or 32-bit into 64-bit. Without widening, a naive accumulation of 16-bit values overflows after 256 additions. ARM's checksum code carefully chooses vpadalq to avoid this.
- **Compiler auto-vectorization rarely produces multi-accumulator code.** This is almost always a manual optimization. Some compilers with aggressive unrolling (-O3 -funroll-loops) may produce 2-way accumulation, but 4-way requires explicit coding.
