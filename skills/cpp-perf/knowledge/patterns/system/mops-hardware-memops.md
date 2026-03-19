---
name: MOPS Hardware Memory Operations (ARMv8.8+ cpyfp/setp)
source: optimized-routines string/aarch64/memcpy-mops.S
layers: [system, microarchitecture]
platforms: [arm]
keywords: [MOPS, cpyfp, cpyfm, cpyfe, setp, setm, sete, ARMv8.8, hardware memcpy, memset]
---

## Problem

Software memcpy/memset implementations require extensive size-based dispatch code: separate paths for 0-16 bytes, 16-64 bytes, 64-128 bytes, large copies with prefetch, alignment handling, and tail processing. ARM's optimized-routines memcpy.S is ~200 lines of carefully crafted assembly. This complexity exists because no single instruction sequence is optimal for all sizes.

ARMv8.8 (2021) introduced MOPS (Memory Operations) -- hardware instructions that reduce memcpy and memset to exactly 3 instructions each. The CPU microcode handles all size dispatch, alignment optimization, cache prefetch, and pipeline scheduling internally. The hardware can optimize for the specific core's microarchitecture in ways software cannot.

## Detection

- Custom memcpy/memset implementations that could be replaced by hardware MOPS
- Performance-sensitive code on ARMv8.8+ platforms (Cortex-X3+, Neoverse V2+, Apple M3+)
- Code that does runtime dispatch between multiple memcpy variants based on size
- `CPUID`/`getauxval` checks for `HWCAP2_MOPS` feature flag

## Transformation

### MOPS memcpy: 3 instructions for ANY size

```
// MOPS memcpy (from optimized-routines memcpy-mops.S)
// x0 = dst, x1 = src, x2 = count
// Total: 5 instructions including setup and return

memcpy_mops:
    mov   x3, x0               // save original dst for return value

    // The entire copy in 3 instructions:
    cpyfp [x0]!, [x1]!, x2!    // Copy Forward Prologue
                                 //   - handles alignment
                                 //   - copies initial bytes to reach aligned boundary
                                 //   - updates x0, x1, x2

    cpyfm [x0]!, [x1]!, x2!    // Copy Forward Main
                                 //   - bulk copy of aligned cache-line-sized chunks
                                 //   - hardware handles prefetch and pipeline optimization
                                 //   - updates x0, x1, x2

    cpyfe [x0]!, [x1]!, x2!    // Copy Forward Epilogue
                                 //   - handles remaining tail bytes
                                 //   - updates x0, x1, x2 (x2 becomes 0)

    mov   x0, x3               // return original dst
    ret
```

### MOPS memset: 3 instructions for ANY size

```
// MOPS memset (from optimized-routines memset-mops.S)
// x0 = dst, x1 = value (byte), x2 = count

memset_mops:
    mov   x3, x0               // save dst for return

    setp  [x0]!, x2!, x1       // Set Prologue -- alignment handling
    setm  [x0]!, x2!, x1       // Set Main -- bulk fill
    sete  [x0]!, x2!, x1       // Set Epilogue -- tail handling

    mov   x0, x3
    ret
```

### MOPS memmove: hardware-managed overlap detection

```
// MOPS memmove handles overlapping src/dst automatically
// The hardware determines copy direction (forward or backward)

memmove_mops:
    mov   x3, x0

    // Use cpyp/cpym/cpye for potentially overlapping copies
    // Hardware detects overlap and chooses forward or backward copy
    cpyp  [x0]!, [x1]!, x2!    // Copy Prologue (direction auto-detected)
    cpym  [x0]!, [x1]!, x2!    // Copy Main
    cpye  [x0]!, [x1]!, x2!    // Copy Epilogue

    mov   x0, x3
    ret
```

### Runtime feature detection and dispatch

```cpp
#include <cstring>
#include <cstddef>

#ifdef __linux__
#include <sys/auxv.h>
#ifndef HWCAP2_MOPS
#define HWCAP2_MOPS (1UL << 43)
#endif
#endif

// Check MOPS availability at startup
static bool has_mops() {
#ifdef __linux__
    unsigned long hwcap2 = getauxval(AT_HWCAP2);
    return (hwcap2 & HWCAP2_MOPS) != 0;
#elif defined(__APPLE__)
    // Apple M3+ supports MOPS; check via sysctl
    // (Apple does not expose HWCAP2, use sysctlbyname instead)
    return false;  // implement platform-specific check
#else
    return false;
#endif
}

// MOPS memcpy using inline assembly
static void mops_memcpy(void* dst, const void* src, size_t n) {
    // cpyfp/cpyfm/cpyfe modify all three operand registers
    register void* d asm("x0") = dst;
    register const void* s asm("x1") = src;
    register size_t count asm("x2") = n;

    asm volatile(
        "cpyfp [%0]!, [%1]!, %2!\n"
        "cpyfm [%0]!, [%1]!, %2!\n"
        "cpyfe [%0]!, [%1]!, %2!\n"
        : "+r"(d), "+r"(s), "+r"(count)
        :
        : "memory"
    );
}

// MOPS memset using inline assembly
static void mops_memset(void* dst, int value, size_t n) {
    register void* d asm("x0") = dst;
    register size_t count asm("x1") = n;
    register uint64_t val asm("x2") = (uint8_t)value;

    asm volatile(
        "setp [%0]!, %1!, %2\n"
        "setm [%0]!, %1!, %2\n"
        "sete [%0]!, %1!, %2\n"
        : "+r"(d), "+r"(count)
        : "r"(val)
        : "memory"
    );
}

// Dispatch function: use MOPS when available
void* fast_memcpy(void* dst, const void* src, size_t n) {
    static const bool use_mops = has_mops();
    if (use_mops) {
        mops_memcpy(dst, src, n);
        return dst;
    }
    return memcpy(dst, src, n);
}
```

### MOPS instruction variants

| Operation | Prologue | Main | Epilogue | Purpose |
|-----------|----------|------|----------|---------|
| Copy Forward | `cpyfp` | `cpyfm` | `cpyfe` | Non-overlapping memcpy |
| Copy (any overlap) | `cpyp` | `cpym` | `cpye` | memmove (auto direction) |
| Copy Forward Write-Through | `cpyfpwt` | `cpyfmwt` | `cpyfewt` | Write-through cache policy |
| Copy Forward Read-Once | `cpyfprt` | `cpyfmrt` | `cpyfert` | Streaming read (non-temporal) |
| Set | `setp` | `setm` | `sete` | memset |
| Set with Tag | `setgp` | `setgm` | `setge` | memset + MTE tag setting |

### GCC/Clang compiler support

```cpp
// GCC 12+ and Clang 15+ can generate MOPS instructions with:
//   -march=armv8.8-a+mops  (or -march=armv9.3-a)
//
// With these flags, __builtin_memcpy and __builtin_memset may
// directly emit MOPS instructions instead of calling library functions.

// Clang 16+: -mops flag explicitly enables MOPS codegen
// The compiler can inline memcpy/memset as 3 MOPS instructions

void example(void* dst, const void* src, size_t n) {
    // With -march=armv8.8-a+mops, this may compile to cpyfp/cpyfm/cpyfe
    __builtin_memcpy(dst, src, n);
}
```

## Expected Impact

| Scenario | Software memcpy | MOPS memcpy | Notes |
|----------|----------------|-------------|-------|
| Small copies (< 64B) | 10-20 instructions (dispatch) | 5 instructions (fixed) | Eliminates dispatch overhead |
| Medium copies (64B-4KB) | Near-optimal (tuned loop) | Comparable or faster | HW can use wider internal buses |
| Large copies (> 4KB) | Near-optimal (prefetch + STP) | Comparable | Both memory-bandwidth limited |
| Code size | ~200 lines assembly | 5 instructions | 40x code size reduction |
| Maintenance | Architecture-specific tuning needed | Future-proof | HW handles microarch optimization |

The primary benefit of MOPS is not raw throughput for large copies (both approach memory bandwidth limits), but:
1. **Eliminating dispatch overhead for small copies** (most common in practice)
2. **Future-proofing:** new CPU cores can optimize MOPS microcode without recompiling
3. **Code simplicity:** 5 instructions vs 200 lines of hand-tuned assembly

## Caveats

- **ARMv8.8 / ARMv9.3 required.** MOPS is NOT available on Neoverse N1/V1, Cortex-A78, or Apple M1/M2. Check `HWCAP2_MOPS` at runtime. As of early 2025, available on: Cortex-X3, Cortex-A715, Neoverse V2, and later.
- **All three instructions (prologue/main/epilogue) MUST execute as a group.** You cannot use only `cpyfp` without `cpyfm` and `cpyfe`. The architecture requires all three in sequence. An exception between them causes the copy to be restarted from the prologue.
- **Interrupts are handled correctly.** MOPS is interruptible -- the CPU can take an interrupt mid-copy and resume correctly. The register updates (`!` suffix) track progress. This is a significant advantage over non-interruptible DMA or `rep movsb` on x86.
- **MOPS may not be faster for ALL sizes on ALL cores.** Some implementations optimize MOPS better than others. On first-generation MOPS cores, software memcpy may still be faster for specific size ranges. Profile on your target hardware.
- **The `cpyfp`/`cpyfm`/`cpyfe` sequence is for NON-overlapping copies.** For potentially overlapping copies (memmove), use `cpyp`/`cpym`/`cpye` instead. Using the wrong variant with overlapping buffers produces incorrect results.
- **glibc 2.37+ includes MOPS-based memcpy/memset.** On recent ARM Linux distributions, the system libc already uses MOPS when available via `ifunc` dispatch. Custom implementations are mainly useful for bare-metal, kernel, or static-linked binaries.
