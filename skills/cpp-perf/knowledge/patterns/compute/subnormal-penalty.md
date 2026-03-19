---
name: Subnormal (Denormal) Floating-Point Penalty
source: perf-book Ch.12, Intel Optimization Manual, ARM Architecture Reference Manual
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [subnormal, denormal, flush to zero, FTZ, DAZ, FPCR, MXCSR, microcode assist, ffast-math]
---

## Problem

IEEE 754 subnormal (denormal) numbers are values smaller than the minimum normal floating-point number (for float: ~1.18e-38). They provide graceful underflow by trading mantissa bits for smaller exponents. However, most CPU hardware does not have a fast path for subnormal arithmetic -- operations on subnormals trigger **microcode assists**, which are 10-100x slower than normal FP operations.

The penalty mechanism differs by architecture:
- **x86 (Intel/AMD):** subnormal operands or results cause a microcode assist, stalling the pipeline for ~100-160 cycles per operation. The assist is invisible in normal profiling but appears as the `FP_ASSIST.ANY` performance counter.
- **ARM:** behavior depends on FPCR (Floating-Point Control Register) settings. Without FTZ (Flush-To-Zero) enabled, the processor either takes a microcode assist or traps to software emulation, costing 10-50x the normal latency.

Subnormals commonly appear in:
- **Signal processing:** filter outputs decaying toward zero (IIR filters, reverb tails)
- **Physics simulations:** particles with near-zero velocity or force
- **Ray tracing:** accumulated color/light values approaching zero through multiple bounces
- **Neural network inference:** activations or gradients in very deep networks
- **Any iterative computation** where values multiply by factors < 1.0 repeatedly

```cpp
// Typical scenario: IIR filter where output decays toward subnormal range
float y = 0.0f;
for (int i = 0; i < N; i++) {
    y = alpha * y + (1 - alpha) * x[i];  // when x[i] ≈ 0, y decays toward subnormal
    output[i] = y;
    // Once y enters subnormal range (~1e-38), each FP op costs 100+ cycles
}
```

## Detection

**Source-level indicators:**
- Iterative loops where a value is repeatedly multiplied by a factor < 1.0
- Feedback systems (IIR filters, PID controllers) with inputs that can go to zero
- Accumulation of many small FP values that may individually underflow
- No explicit clamping or zeroing of small values

**Profile-level indicators (x86):**
```bash
perf stat -e fp_assist.any ./myapp
# Any non-trivial count indicates subnormal penalty
# Expect 0 in well-optimized code
```

**Profile-level indicators (ARM):**
- Check FPCR.FZ bit: if 0, subnormals are handled in software
- On Linux: sudden throughput drops in FP-heavy loops without apparent cause
- Compare performance with and without `-ffast-math` -- large unexplained delta suggests subnormals

**Runtime detection:**
```cpp
#include <cmath>
#include <cfloat>

void check_for_subnormals(const float* data, int n) {
    int count = 0;
    for (int i = 0; i < n; i++) {
        if (std::fpclassify(data[i]) == FP_SUBNORMAL) count++;
    }
    if (count > 0) fprintf(stderr, "WARNING: %d subnormal values detected\n", count);
}
```

## Transformation

### Strategy 1: Enable FTZ+DAZ globally via compiler flags

The simplest fix -- tell the compiler to flush subnormals to zero:

```bash
# GCC/Clang: -ffast-math implies FTZ+DAZ
g++ -O3 -ffast-math program.cpp

# Or more targeted (GCC):
g++ -O3 -mdaz-ftz program.cpp

# Clang: -fdenormal-fp-math=positive-zero
clang++ -O3 -fdenormal-fp-math=positive-zero program.cpp
```

`-ffast-math` enables FTZ+DAZ as part of its broader FP relaxations. If you only want subnormal flushing without other `-ffast-math` effects, use the more targeted flags.

### Strategy 2: Set FTZ+DAZ at runtime via MXCSR (x86)

```cpp
#include <xmmintrin.h>  // for _MM_SET_FLUSH_ZERO_MODE
#include <pmmintrin.h>  // for _MM_SET_DENORMALS_ZERO_MODE

void enable_ftz_daz() {
    _MM_SET_FLUSH_ZERO_MODE(_MM_FLUSH_ZERO_ON);       // FTZ: flush results to zero
    _MM_SET_DENORMALS_ZERO_MODE(_MM_DENORMALS_ZERO_ON); // DAZ: treat inputs as zero
}
// Call at the start of main() or each thread entry point
// Note: MXCSR is per-thread, so each thread must set it independently
```

FTZ flushes subnormal **results** to zero; DAZ treats subnormal **inputs** as zero. Both should be enabled together for maximum effect.

### Strategy 3: Set FTZ via FPCR (ARM AArch64)

```cpp
#include <fenv.h>

void enable_ftz_arm() {
    // Read FPCR
    uint64_t fpcr;
    asm volatile("mrs %0, fpcr" : "=r"(fpcr));
    // Set FZ bit (bit 24) for flush-to-zero
    fpcr |= (1UL << 24);
    asm volatile("msr fpcr, %0" : : "r"(fpcr));
}
```

On ARM, the FPCR.FZ bit controls flush-to-zero for both inputs and outputs in a single bit.

### Strategy 4: Algorithmic fix -- clamp small values

When FTZ is not acceptable (e.g., strict IEEE compliance required), add explicit clamping:

```cpp
// IIR filter with subnormal prevention
float y = 0.0f;
const float EPSILON = 1e-30f;  // well above subnormal range
for (int i = 0; i < N; i++) {
    y = alpha * y + (1 - alpha) * x[i];
    if (std::abs(y) < EPSILON) y = 0.0f;  // clamp before it enters subnormal
    output[i] = y;
}
```

Or use a branchless approach (DC offset technique, common in audio DSP):

```cpp
// Add and subtract a small constant to flush subnormals without branching
const float DC_OFFSET = 1e-25f;
y = alpha * (y + DC_OFFSET) + (1 - alpha) * x[i] - alpha * DC_OFFSET;
```

## Expected Impact

- **Worst case (dense subnormals):** 10-100x slowdown eliminated. An IIR filter processing silence can go from ~160 cycles/sample to ~4 cycles/sample.
- **Typical signal processing:** 2-10x speedup in tails/decay sections where subnormals accumulate.
- **Physics simulations:** eliminates sporadic latency spikes when particles approach zero velocity.
- **Audio processing:** critical for real-time audio where subnormal-induced slowdowns cause buffer underruns and audible glitches.

## Caveats

- **IEEE 754 compliance:** FTZ+DAZ violates strict IEEE 754 semantics. Some numerical algorithms depend on gradual underflow for correctness (e.g., certain implementations of `hypot`, compensated summation). Verify that your algorithms tolerate flushing to zero.
- **Per-thread state:** on x86, MXCSR is per-thread. Each thread in a thread pool must set FTZ+DAZ independently. Forgetting this causes intermittent subnormal penalties in worker threads.
- **Library interactions:** calling third-party libraries that modify MXCSR/FPCR can reset your FTZ settings. After returning from such calls, re-establish the desired mode.
- **`-ffast-math` side effects:** this flag also enables `-fno-math-errno`, `-freciprocal-math`, `-ffinite-math-only`, `-fno-signed-zeros`, `-fno-trapping-math`, `-fassociative-math`, and `-ffp-contract=fast`. If you only want FTZ+DAZ, use the runtime approach or targeted compiler flags.
- **ARM variation:** some older ARM cores (Cortex-A53) handle subnormals in hardware without penalty. Always profile to confirm the penalty exists on your target before adding complexity.
- **Denormal inputs from external sources:** even with DAZ enabled, data read from files or network may contain subnormals. Consider pre-filtering input data.
