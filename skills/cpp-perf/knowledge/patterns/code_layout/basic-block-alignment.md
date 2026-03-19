---
name: Basic Block Alignment for Cache Line Efficiency
source: perf-book Ch.11, Section 11-4 Basic Block Alignment
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [alignment, cache line, NOP, loop alignment, code_align, cache line straddling, fetch block, basic block, hot loop, padding]
---

## Problem

When a hot loop's machine code straddles a cache line boundary (typically 64 bytes), the processor must fetch from two cache lines for every iteration, even if the loop body is small enough to fit in one. This can measurably degrade performance even in microbenchmarks where I-cache capacity is not an issue.

Example from the book: a vectorized loop of 5 instructions (26 bytes) starts at offset `0x4046b0` and ends at `0x4046ca`. This spans cache lines `0x80-0xBF` and `0xC0-0xFF`. By inserting a 16-byte NOP before the loop to shift it forward, the entire loop fits within a single cache line.

```
Default layout:                         Improved layout:
Cache line 0x80-0xBF:                   Cache line 0x80-0xBF:
  [preamble] [loop start...]              [preamble] [16B NOP padding]
Cache line 0xC0-0xFF:                   Cache line 0xC0-0xFF:
  [...loop end]                           [entire loop fits here]
```

The performance impact is visible even in a microbenchmark running nothing but this hot loop, because the root cause involves microarchitectural details of how the fetch unit and uop-cache interact with cache line boundaries.

## Detection

**Source-level indicators:**
- Small, tight, hot loops (especially vectorized loops with SIMD instructions)
- Loops that execute millions of iterations where even small per-iteration overhead matters

**Profile-level indicators:**
- TMA: elevated `Frontend_Bound` in a function dominated by a small loop
- Unexplained performance variance between builds (the same code compiled with different surrounding code may shift alignment and change performance)
- Performance changes when unrelated code is added or removed (phantom regressions caused by alignment shifts)

**Disassembly clues:**
- Check the address of the first instruction of the hot loop
- If `loop_start_address % 64` + `loop_size_in_bytes` > 64, the loop straddles a cache line boundary
- Look for compiler-inserted NOPs before loop headers (LLVM inserts NOPs to align loops to 16B by default)

## Transformation

### Clang `[[clang::code_align()]]` attribute (recommended)

Fine-grained, source-level control. Aligns the loop to the specified boundary:

```cpp
void benchmark_func(int* a) {
  [[clang::code_align(64)]]
  for (int i = 0; i < 32; ++i)
    a[i] += 1;
}
```

This instructs the compiler to insert NOP padding before the loop so that the first instruction of the loop starts at a 64-byte-aligned address.

### Inline assembly fallback (portable across compilers)

```cpp
asm(".align 64;");
for (int i = 0; i < N; ++i) {
    // hot loop body
}
```

### Compiler flags (use with caution)

- LLVM default: loops aligned to 16-byte boundaries
- `-mllvm -align-all-blocks=5`: aligns every basic block to 32-byte boundary (LLVM)

These flags affect the entire translation unit and are NOT recommended for production use. They can improve some loops while worsening others by increasing total code size.

## Expected Impact

- **Measurable in microbenchmarks**: Even a single hot loop can show a few percent speedup when alignment eliminates a cache line straddle.
- **Typical speedup**: 0-5%, highly variable and dependent on the specific loop size and its position relative to cache line boundaries.
- **Greatest impact**: Small loops (< 64 bytes of machine code) that execute at very high iteration counts. Vectorized inner loops are prime candidates because SIMD instructions are often 4-6 bytes each and a few instructions can fill most of a cache line.
- **Alignment noise**: Machine code alignment is one of the main sources of noise in performance measurements. A change in alignment can cause several percent performance swing unrelated to any actual optimization.

## Caveats

- **NOP padding increases code size**: Every alignment directive inserts dead bytes. Aggressive alignment (e.g., aligning all blocks to 32B or 64B) can significantly increase code size, which harms I-cache utilization for the rest of the program.
- **Do NOT use global alignment flags in production**: `-mllvm -align-all-blocks=5` affects every function in the translation unit. Use targeted `[[clang::code_align()]]` only on performance-critical loops.
- **Only matters for hot loops**: If a loop executes a small number of iterations, the one-time cost of fetching an extra cache line is negligible. Focus on loops identified as hot by profiling.
- **Compiler already aligns to 16B**: LLVM's default 16B loop alignment is sufficient in most cases. Only intervene when profiling shows a specific loop with a cache line straddling issue.
- **Architecture-dependent**: Cache line sizes vary (64B on most x86 and ARM, but check). The alignment value should match the target's cache line size.
- **Interacts with function layout**: If the function itself is not aligned, aligning an inner loop may just shift the problem elsewhere. Consider function-level alignment too.
