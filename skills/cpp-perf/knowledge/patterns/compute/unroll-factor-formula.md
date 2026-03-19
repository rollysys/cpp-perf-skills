---
name: Optimal Loop Unroll Factor Formula
source: perf-book Ch.5 (static analysis section)
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [unroll, unroll factor, throughput, latency, ILP, loop unrolling, pipeline, saturation]
---

## Problem

Loop unrolling is one of the most common optimizations, but choosing the wrong unroll factor is worse than not unrolling at all:

- **Under-unrolling:** the CPU's execution units sit idle because each iteration depends on the previous one. The loop is *latency-bound* on the critical dependency chain. Throughput is wasted.
- **Over-unrolling:** too many live variables spill to the stack (register pressure), the loop body exceeds the instruction cache or loop buffer capacity, and the branch predictor has fewer iterations to amortize misprediction costs.

The optimal unroll factor is not a guess. It is a formula:

```
Unroll Factor = Instruction Throughput × Instruction Latency
```

This is the number of **independent** operations that must be in flight simultaneously to fully saturate the execution unit, accounting for the pipeline depth (latency) of each operation.

## Detection

**Source-level indicators:**
- A loop with a single accumulator performing FMA, multiply, or add on a dependency chain
- Manually unrolled loop with an arbitrary unroll factor (often 2 or 4 "because powers of 2")
- Compiler pragma `#pragma unroll(N)` with no justification for the chosen N
- Throughput significantly below the theoretical peak for the operation

**Profile-level indicators:**
- Low IPC despite simple, compute-bound loop (indicates latency-bound)
- TMA shows `Core Bound > Ports Utilization` — execution ports are underutilized
- `llvm-mca` reports throughput bottleneck on a single dependency chain
- Measured throughput per cycle is `1/latency` instead of `throughput` (e.g., 0.25 FMA/cycle instead of 2 FMA/cycle)

## Transformation

### The Formula

For any pipelined functional unit:

```
Optimal Unroll Factor = Throughput (ops/cycle) × Latency (cycles)
```

This represents the number of independent operations needed to fill the pipeline and keep the unit issuing at full throughput every cycle.

### Platform-Specific Values

**ARM Neoverse V1 (server, Graviton 3):**
| Instruction | Throughput | Latency | Unroll Factor |
|-------------|-----------|---------|---------------|
| FMLA (vector) | 2/cycle | 4 cycles | 8 |
| FADD (vector) | 2/cycle | 2 cycles | 4 |
| FMUL (vector) | 2/cycle | 3 cycles | 6 |

**ARM Cortex-A55 (efficiency core, mobile):**
| Instruction | Throughput | Latency | Unroll Factor |
|-------------|-----------|---------|---------------|
| FMLA (vector) | 1/cycle | 6 cycles | 6 |
| FADD (vector) | 1/cycle | 4 cycles | 4 |

**Intel Skylake / Ice Lake:**
| Instruction | Throughput | Latency | Unroll Factor |
|-------------|-----------|---------|---------------|
| VFMADD (256-bit) | 2/cycle | 4 cycles | 8 |
| VADDPS (256-bit) | 2/cycle | 4 cycles | 8 |
| VMULPS (256-bit) | 2/cycle | 4 cycles | 8 |

### Example: GEMM inner loop

```cpp
// Before: single accumulator, latency-bound
// FMA latency = 4 cycles, throughput = 2/cycle
// Achieved: 1 FMA every 4 cycles = 0.25 FMA/cycle (12.5% utilization)
void gemm_naive(const float* A, const float* B, float* C, int N) {
    for (int i = 0; i < N; i++)
      for (int j = 0; j < N; j++)
        for (int k = 0; k < N; k++)
          C[i*N+j] += A[i*N+k] * B[k*N+j];  // single accumulator
}

// After: 8 independent accumulators (unroll = 2 tp × 4 lat = 8)
// Achieved: 2 FMA/cycle (100% utilization, if not memory-bound)
void gemm_unrolled(const float* A, const float* B, float* C, int N) {
    for (int i = 0; i < N; i++)
      for (int j = 0; j < N; j += 8) {  // unroll j by 8
        float c0=0, c1=0, c2=0, c3=0, c4=0, c5=0, c6=0, c7=0;
        for (int k = 0; k < N; k++) {
          float a = A[i*N+k];
          c0 += a * B[k*N+j+0];
          c1 += a * B[k*N+j+1];
          c2 += a * B[k*N+j+2];
          c3 += a * B[k*N+j+3];
          c4 += a * B[k*N+j+4];
          c5 += a * B[k*N+j+5];
          c6 += a * B[k*N+j+6];
          c7 += a * B[k*N+j+7];
        }
        C[i*N+j+0] += c0; C[i*N+j+1] += c1;
        C[i*N+j+2] += c2; C[i*N+j+3] += c3;
        C[i*N+j+4] += c4; C[i*N+j+5] += c5;
        C[i*N+j+6] += c6; C[i*N+j+7] += c7;
      }
}
```

### Example: Vectorized reduction with NEON

```cpp
// Before: single vector accumulator, latency-bound on FMLA (4 cycles)
// On Neoverse V1: throughput = 2 FMLA/cycle, but achieved = 0.25 FMLA/cycle
float32x4_t sum = vdupq_n_f32(0);
for (int i = 0; i < N; i += 4) {
    float32x4_t a = vld1q_f32(data + i);
    sum = vfmaq_f32(sum, a, a);  // sum depends on previous sum
}

// After: 8 independent accumulators (unroll = 2 × 4 = 8)
float32x4_t s0 = vdupq_n_f32(0), s1 = vdupq_n_f32(0);
float32x4_t s2 = vdupq_n_f32(0), s3 = vdupq_n_f32(0);
float32x4_t s4 = vdupq_n_f32(0), s5 = vdupq_n_f32(0);
float32x4_t s6 = vdupq_n_f32(0), s7 = vdupq_n_f32(0);
for (int i = 0; i < N; i += 32) {
    float32x4_t a0 = vld1q_f32(data + i +  0);
    float32x4_t a1 = vld1q_f32(data + i +  4);
    float32x4_t a2 = vld1q_f32(data + i +  8);
    float32x4_t a3 = vld1q_f32(data + i + 12);
    float32x4_t a4 = vld1q_f32(data + i + 16);
    float32x4_t a5 = vld1q_f32(data + i + 20);
    float32x4_t a6 = vld1q_f32(data + i + 24);
    float32x4_t a7 = vld1q_f32(data + i + 28);
    s0 = vfmaq_f32(s0, a0, a0);
    s1 = vfmaq_f32(s1, a1, a1);
    s2 = vfmaq_f32(s2, a2, a2);
    s3 = vfmaq_f32(s3, a3, a3);
    s4 = vfmaq_f32(s4, a4, a4);
    s5 = vfmaq_f32(s5, a5, a5);
    s6 = vfmaq_f32(s6, a6, a6);
    s7 = vfmaq_f32(s7, a7, a7);
}
// Reduce: s0 += s1 + s2 + ... + s7
s0 = vaddq_f32(s0, s1); s2 = vaddq_f32(s2, s3);
s4 = vaddq_f32(s4, s5); s6 = vaddq_f32(s6, s7);
s0 = vaddq_f32(s0, s2); s4 = vaddq_f32(s4, s6);
s0 = vaddq_f32(s0, s4);
float result = vaddvq_f32(s0);
```

## Expected Impact

- **Latency-bound to throughput-bound transition:** the speedup equals the unroll factor when the loop was purely latency-bound. On Neoverse V1 with single-accumulator FMA: unroll 8 → up to 8x speedup.
- **In practice:** 3-6x is typical because memory access and other overhead limit the achievable throughput.
- **Diminishing returns beyond the formula:** unrolling beyond `throughput × latency` provides zero additional throughput and starts hurting from register pressure.

## Caveats

- **Register pressure:** on ARM AArch64, there are 32 SIMD registers. Unrolling by 8 with 2 registers per accumulator (data + accumulator) uses 16 registers. Unrolling by 16 would need 32 registers, leaving none for temporaries. The formula gives the *minimum* unroll factor; the *maximum* is constrained by available registers.
- **I-cache pressure:** unrolling a large loop body can evict other hot code from the I-cache. For loops with many instructions per body, the I-cache miss penalty can offset the throughput gain.
- **Compiler auto-unrolling:** compilers already unroll loops, but they are conservative. Check the actual unroll factor with `-fopt-info-vec` (GCC) or `-Rpass=loop-unroll` (Clang) before manually unrolling.
- **Memory-bound loops:** if the loop is bottlenecked on memory bandwidth (not latency-bound on compute), unrolling does not help. Check the roofline model first.
- **A55 vs V1 values differ significantly:** using the V1 unroll factor (8) on A55 (which needs 6) wastes 2 registers for no throughput gain. Always use platform-specific throughput and latency values.
- **The formula assumes independent operations:** if there is a data dependency between the unrolled iterations (e.g., a scan/prefix sum), unrolling alone does not help. The dependency chain must be broken algebraically.
