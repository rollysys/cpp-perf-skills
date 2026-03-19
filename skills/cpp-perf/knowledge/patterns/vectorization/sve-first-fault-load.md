---
name: SVE First-Fault Load (Alignment-Free Safe Speculation)
source: optimized-routines string/aarch64/experimental/strlen-sve.S
layers: [microarchitecture]
platforms: [arm]
keywords: [SVE, first fault, ldff1b, page boundary, alignment free, predicate, brkb, brka, incp]
---

## Problem

String operations (strlen, strchr, memchr) process data of unknown length. The fundamental challenge: you want to load a full vector width (potentially 16-64 bytes with SVE) but the string might end near a page boundary. Loading past the end could cross into an unmapped page and cause a segfault.

Traditional solutions:
1. **Align to page boundary first, then use full loads** -- requires an alignment prologue with scalar byte-by-byte processing (slow for short strings)
2. **Check remaining bytes before each load** -- requires knowing the string length in advance (chicken-and-egg for strlen)

SVE's first-fault loads (`ldff1b`) solve this at the hardware level: the CPU attempts to load the full vector but automatically suppresses faults for any element that would cross a page boundary. The `rdffrs` instruction tells you which elements actually loaded successfully.

ARM's SVE strlen is 11 instructions total vs ~60 for the NEON equivalent, primarily because first-fault loads eliminate all alignment handling.

## Detection

- String/memory scanning code with explicit page-boundary alignment prologues
- NEON code that aligns to 16-byte boundaries before the main loop, processing leading bytes one-by-one
- Functions that check `(ptr & 0xFFF) > (4096 - vector_width)` to avoid page-crossing loads
- Any pattern where "we don't know the length, but need to load vector-width chunks safely"

## Transformation

### SVE strlen from optimized-routines (annotated)

```
// SVE strlen -- 11 instructions total
// x0 = input string pointer
// Returns: length in x0

strlen_sve:
    mov     x1, #0              // byte offset = 0
    setffr                      // initialize first-fault register (all-ones)
    ptrue   p0.b                // p0 = all-true predicate (all lanes active)

.Lloop:
    // First-fault load: load bytes starting at x0+x1
    // If any element would cross a page boundary, HW suppresses it
    ldff1b  z0.b, p0/z, [x0, x1]

    // Read first-fault result: which elements actually loaded?
    rdffrs  p1.b, p0/z          // p1 = mask of successfully loaded elements

    // Did all elements load? (no page boundary hit)
    b.nlast .Lhit_boundary      // if not all loaded, handle partial

    // Compare loaded bytes against zero (NUL terminator)
    cmpeq   p2.b, p0/z, z0.b, #0   // p2 = mask of NUL bytes

    // If no NUL found, advance and continue
    b.none  .Ladvance           // no NUL? continue loop

    // NUL found -- find position of first NUL
    brkb    p2.b, p0/z, p2.b   // break after first true in p2
    incp    x1, p2.b            // x1 += count of true elements before break
    mov     x0, x1              // return length
    ret

.Ladvance:
    incb    x1                  // x1 += vector_length_in_bytes
    setffr                      // reset first-fault register for next iteration
    b       .Lloop

.Lhit_boundary:
    // Partial load succeeded (page boundary hit)
    // Compare only the elements that actually loaded
    cmpeq   p2.b, p1/z, z0.b, #0   // use p1 (valid elements) as governing predicate
    b.none  .Lrealign               // no NUL in valid portion -> realign and retry
    // ... find position as above
```

### Key SVE instructions for first-fault pattern

| Instruction | Purpose |
|-------------|---------|
| `setffr` | Initialize/reset the first-fault register to all-ones |
| `ldff1b z0.b, p0/z, [base, offset]` | First-fault load: suppress faults for elements crossing page boundary |
| `rdffrs p1.b, p0/z` | Read first-fault result: p1 = which elements loaded successfully |
| `cmpeq p2.b, p0/z, z0.b, #0` | Compare each byte to zero, result in predicate p2 |
| `brkb p2.b, p0/z, p2.b` | Set all bits after first true to false (isolate first match) |
| `brka p2.b, p0/z, p2.b` | Set all bits before first true to false (and the true bit stays) |
| `incp x0, p.b` | Add popcount of predicate to scalar register |
| `lasta x0, p, z0.b` | Extract the element at the first active predicate position |
| `incb x0` | Add SVE vector length in bytes to x0 (VL-agnostic increment) |

### C intrinsics equivalent (ACLE)

```cpp
#include <arm_sve.h>

size_t sve_strlen(const char* s) {
    size_t offset = 0;
    svbool_t all_true = svptrue_b8();

    while (1) {
        // First-fault load: safe even near page boundaries
        svuint8_t data = svldff1_u8(all_true, (const uint8_t*)(s + offset));

        // Check which elements actually loaded
        svbool_t valid = svrdffr();

        // Compare to zero within valid elements
        svbool_t matches = svcmpeq_n_u8(valid, data, 0);

        if (svptest_any(valid, matches)) {
            // Found NUL: count elements before first match
            svbool_t before_match = svbrkb_z(valid, matches);
            return offset + svcntp_b8(valid, before_match);
        }

        if (!svptest_last(all_true, valid)) {
            // Page boundary hit, partial load -- re-align
            // (simplification: just advance by valid count)
            offset += svcntp_b8(all_true, valid);
            svsetffr();  // reset for next load
            continue;
        }

        // All elements loaded, no NUL found -- advance full vector
        offset += svcntb();  // VL-agnostic: works for any SVE width
        svsetffr();
    }
}
```

### SVE memchr using first-fault (from optimized-routines)

```cpp
#include <arm_sve.h>

void* sve_memchr(const void* s, int c, size_t n) {
    const uint8_t* p = (const uint8_t*)s;
    uint8_t target = (uint8_t)c;
    size_t offset = 0;

    while (offset < n) {
        // Create predicate for remaining bytes
        svbool_t pg = svwhilelt_b8_u64(offset, n);

        // First-fault load (safe near page boundaries)
        svuint8_t data = svldff1_u8(pg, p + offset);
        svbool_t valid = svrdffr();

        // Compare within valid elements
        svbool_t match = svcmpeq_n_u8(valid, data, target);

        if (svptest_any(valid, match)) {
            svbool_t first = svbrka_z(valid, match);
            size_t pos = svcntp_b8(valid, first) - 1;
            return (void*)(p + offset + pos);
        }

        offset += svcntp_b8(pg, valid);
        svsetffr();
    }
    return NULL;
}
```

## Expected Impact

| Metric | NEON strlen | SVE strlen | Improvement |
|--------|------------|------------|-------------|
| Instructions (core loop) | ~60 | ~11 | 5.5x fewer |
| Alignment prologue | 15-20 instructions | 0 (eliminated) | Eliminated |
| Branches per iteration | 3-4 | 2 | ~50% fewer |
| Short strings (< 16B) | Scalar path, slow | Same fast path | 2-4x faster |
| Throughput (long strings, 256-bit SVE) | 16B/cycle | 32B/cycle | 2x |
| Throughput (long strings, 512-bit SVE) | 16B/cycle | 64B/cycle | 4x |

The improvement scales with SVE vector length: wider SVE implementations process more bytes per cycle without any code change. This is VL-agnostic programming -- the same binary runs optimally on 128-bit, 256-bit, and 512-bit SVE implementations.

## Caveats

- **SVE is not universally available.** As of 2025, SVE is present on Neoverse V1/V2/N2, Cortex-A510/A710/A715/X2/X3/X4, and Graviton 3/4. It is NOT available on Cortex-A76/A77/A78 or Apple Silicon. Runtime feature detection via `getauxval(AT_HWCAP)` with `HWCAP_SVE` is required.
- **First-fault loads are NOT available in NEON.** This pattern is SVE-only. NEON code must use explicit alignment prologues or masked loads.
- **`setffr` must be called before each `ldff1b`.** Forgetting to reset the first-fault register causes stale results from previous iterations.
- **The page-boundary hit path is slow.** When `rdffrs` indicates a partial load, the code must re-align or handle the boundary. This path is rarely taken (once per 4KB page) but must be correct.
- **SVE vector length is implementation-defined** (128-2048 bits in 128-bit increments). Code must use VL-agnostic idioms (`incb`, `svcntb()`, `svptrue_b8()`) rather than assuming a specific width.
- **Compiler support:** GCC 10+ and Clang 12+ support SVE ACLE intrinsics. Use `-march=armv8-a+sve` or `-march=armv9-a`.
