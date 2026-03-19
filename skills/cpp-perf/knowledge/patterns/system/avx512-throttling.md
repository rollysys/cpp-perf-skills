---
name: AVX-512 Frequency Throttling on Pre-Ice Lake x86
source: perf-book Ch.12
layers: [system, microarchitecture]
platforms: [x86]
keywords: [AVX-512, frequency, throttle, downclock, P1 license, P2 license, Ice Lake, prefer-vector-width]
---

## Problem

On Intel Skylake-SP, Skylake-X, Cascade Lake, and similar pre-Ice Lake microarchitectures, executing AVX-512 instructions causes the CPU core to **downclock** by 100-300 MHz. This is not a bug — it is by design, because the 512-bit execution units draw significantly more power, and the voltage must be raised to sustain them, which requires lowering frequency to stay within the power envelope.

Intel defines three frequency "licenses":

| License | Trigger | Frequency Drop | Duration |
|---------|---------|---------------|----------|
| **L0 (base)** | SSE, AVX-128 | None | - |
| **L1 (AVX-512 light)** | AVX-512 integer, logic, permute | ~100 MHz | ~1 ms to ramp back |
| **L2 (AVX-512 heavy)** | AVX-512 FP multiply, FMA | ~200-300 MHz | ~1 ms to ramp back |

The critical trap: **the frequency drop affects ALL code on that core, not just the AVX-512 instructions.** If your program runs AVX-512 FMA for 10 microseconds and then runs scalar code for 1 ms, the scalar code runs at the reduced frequency for the entire 1 ms ramp-back period.

This means AVX-512 code can be **net slower** than AVX2 code on mixed workloads:
- AVX-512 FMA doubles the work per instruction (512 vs 256 bits)
- But the core runs at 200 MHz lower frequency (e.g., 3.8 GHz → 3.6 GHz = 5% slower)
- And surrounding scalar/AVX2 code also runs 5% slower
- Net effect: negative or marginal speedup despite 2x wider SIMD

## Detection

**Source-level indicators:**
- Use of `_mm512_*` intrinsics or `-mavx512f` enabling compiler auto-vectorization to 512-bit width
- Code compiled with `-march=skylake-avx512` or `-march=cascadelake`
- Mixed AVX-512 and scalar/AVX2 code in the same program

**Profile-level indicators:**
- `perf stat` shows lower GHz during AVX-512 sections vs AVX2 sections
- Speedup from AVX-512 is much less than 2x over AVX2 (expected from double width)
- Multi-threaded AVX-512 code shows worse-than-expected scaling (shared power budget causes more aggressive throttling)

**Diagnostic:**
```bash
# Monitor frequency transitions
perf stat -e cpu/event=0x3c,umask=0x0,name=cpu_clk_unhalted/,\
            cpu/event=0x3c,umask=0x1,name=cpu_clk_ref_unhalted/ ./benchmark

# Check if AVX-512 is causing downclocking
# Effective GHz = cpu_clk_unhalted / cpu_clk_ref_unhalted * base_freq
# If this drops during AVX-512 sections, you're being throttled

# Direct frequency monitoring
turbostat --show Core,Bzy_MHz,Avg_MHz -- ./benchmark
```

## Transformation

### Fix 1: Cap vector width to 256-bit

Force the compiler to use AVX2 (256-bit) instead of AVX-512, avoiding the frequency penalty entirely:

```bash
# Clang/GCC: cap auto-vectorization width
clang -O2 -march=skylake-avx512 -mprefer-vector-width=256 -c hot_module.cpp
gcc   -O2 -march=skylake-avx512 -mprefer-vector-width=256 -c hot_module.cpp

# This enables AVX-512 *instruction set* (new instructions like vpternlog,
# vpopcnt, vpermi2) but restricts vector width to 256-bit.
# You get the new instructions without the frequency penalty.
```

This is the recommended default for Skylake-SP and Cascade Lake. You get access to useful AVX-512 instructions (ternary logic, conflict detection, population count) at 256-bit width, without triggering the frequency license change.

### Fix 2: Runtime CPU detection

For libraries that must support multiple CPU generations, detect the CPU at runtime and only use 512-bit width on Ice Lake+ where throttling is eliminated or greatly reduced:

```cpp
#include <cpuid.h>

enum class SimdPolicy { AVX2, AVX512_256, AVX512_512 };

SimdPolicy select_simd_policy() {
    uint32_t eax, ebx, ecx, edx;

    // Check CPU family/model
    __cpuid(1, eax, ebx, ecx, edx);
    uint32_t family = ((eax >> 8) & 0xF) + ((eax >> 20) & 0xFF);
    uint32_t model  = ((eax >> 4) & 0xF) | ((eax >> 12) & 0xF0);

    // Ice Lake server = family 6, model 0x6A/0x6C
    // Sapphire Rapids = family 6, model 0x8F
    // These have minimal AVX-512 throttling
    bool is_icelake_plus = (family == 6) &&
        (model == 0x6A || model == 0x6C || model >= 0x8F);

    if (!__builtin_cpu_supports("avx512f"))
        return SimdPolicy::AVX2;
    if (is_icelake_plus)
        return SimdPolicy::AVX512_512;  // full width safe
    return SimdPolicy::AVX512_256;      // use AVX-512 instructions at 256-bit
}
```

### Fix 3: Isolate AVX-512 code to minimize frequency transition overhead

If AVX-512 is genuinely beneficial for a specific kernel, isolate it to minimize the ramp-down/ramp-up cost:

```cpp
// Bad: scattered AVX-512 calls with scalar code between them
for (int batch = 0; batch < n_batches; batch++) {
    prepare_data_scalar(batch);          // runs at L2 frequency
    compute_avx512(data, batch);         // triggers L2 license
    postprocess_scalar(batch);           // still at L2 frequency, paying penalty
}

// Better: batch all AVX-512 work together
prepare_all_data_scalar();               // runs at L0 frequency
for (int batch = 0; batch < n_batches; batch++) {
    compute_avx512(data, batch);         // all at L2 frequency (amortized)
}
// ~1 ms ramp-back to L0
postprocess_all_scalar();                // back at L0 frequency
```

### Fix 4: Use VZEROUPPER to accelerate frequency recovery (AVX2)

While `VZEROUPPER` is primarily needed to avoid SSE/AVX transition penalties, it also signals to the CPU that wide SIMD is no longer in use, which can help frequency recovery:

```cpp
// After AVX-512 section, execute VZEROUPPER
_mm256_zeroupper();
// Or in assembly:
asm volatile("vzeroupper");
```

Note: on AVX-512, the transition penalty architecture is different from AVX2. Modern compilers insert `VZEROUPPER` automatically at ABI boundaries. The frequency recovery benefit is minor.

## Expected Impact

- **`-mprefer-vector-width=256` on Skylake-SP:** 0-15% speedup on mixed workloads compared to full AVX-512, despite using narrower vectors. The frequency recovery more than compensates for the reduced width.
- **On compute-only workloads (100% AVX-512 FMA):** AVX-512 is still faster than AVX2 even with throttling, because the 2x throughput outweighs the ~5-8% frequency reduction.
- **On mixed workloads (AVX-512 kernels + scalar control flow):** AVX-512 can be 5-15% slower than AVX2 due to frequency penalty on the scalar portions.
- **On Ice Lake and later:** throttling is minimal (< 50 MHz). Full 512-bit width is generally beneficial.

## Caveats

- **Ice Lake+ largely fixes this:** Intel Ice Lake (client and server) and Sapphire Rapids have much smaller frequency drops for AVX-512 (< 50 MHz). The guidance to avoid 512-bit width is specific to Skylake/Cascade Lake.
- **AMD Zen4 has no AVX-512 throttling:** AMD's AVX-512 implementation (Zen4+) does not trigger frequency reductions. 512-bit width is always safe on AMD.
- **Multi-core effect:** the frequency license applies to the entire core (and sometimes the entire chip). Running AVX-512 on one thread can reduce frequency for all threads on the same core (SMT) or even all cores on the same die.
- **Server vs client SKUs:** Xeon (server) parts have more aggressive throttling than Core (client) parts because they operate closer to thermal limits. A workload that is net-positive for AVX-512 on a lightly-loaded client chip may be net-negative on a fully-loaded server.
- **Frequency measurement is indirect:** you cannot directly read the frequency license level. You must infer it from `perf stat` cycle counts or `turbostat` measurements. The ramp-back time (~1 ms) is also not precisely specified and varies with thermal state.
- **`-mprefer-vector-width=256` still uses AVX-512 for non-width-dependent operations:** instructions like `vpternlog` (ternary logic), `vpopcntq` (population count), and mask operations are available at 256-bit width and do NOT trigger frequency throttling. This is why `AVX512_256` mode is strictly better than pure AVX2.
