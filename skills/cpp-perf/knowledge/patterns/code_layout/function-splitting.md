---
name: Function Splitting (Hot/Cold Outlining)
source: perf-book Ch.11, Section 11-5 Function Splitting
layers: [microarchitecture, system]
platforms: [arm, x86]
keywords: [function splitting, function outlining, hot path, cold path, noinline, unlikely, I-cache, uop-cache, DSB, text.cold, code size, CFG]
---

## Problem

Large functions with complex control flow graphs often have big blocks of cold code (error handling, logging, diagnostics, fallback paths) interleaved with hot code. This cold code occupies I-cache lines and uop-cache (DSB) entries that could be used by hot instructions, effectively inflating the instruction footprint of the hot path.

```cpp
void foo(bool cond1, bool cond2) {
  // hot path
  if (cond1) {
    /* 50 lines of error handling -- rarely executed */
  }
  // hot path continues
  if (cond2) {
    /* 30 lines of logging -- rarely executed */
  }
  // hot path continues
}
```

In the default layout, all the cold code sits between hot instructions. The CPU Frontend must fetch cache lines filled with cold instructions just to reach the next hot instruction. This wastes I-cache capacity and reduces the density of useful hot code in the cache hierarchy.

## Detection

**Source-level indicators:**
- Functions with > 100 lines containing `if` blocks for error handling, logging, or rare conditions
- Functions with many early-return error checks that have substantial code in the error path
- Functions flagged by the compiler as "too large to inline" that contain both hot loops and cold setup/teardown

**Profile-level indicators:**
- TMA: `Frontend_Bound` > 10% with `ICache_Misses` elevated
- A hot function where profiling shows certain basic blocks have near-zero execution count while surrounding blocks are very hot
- `perf annotate` showing hot instructions separated by large gaps of cold (unsampled) instructions

**Disassembly clues:**
- Large functions (> 200 bytes of machine code) with hot and cold regions interspersed
- Many `jmp` instructions in the hot path that skip over cold blocks

## Transformation

### Manual: outline cold code into separate noinline functions

```cpp
// Before: cold code inline
void foo(bool cond1, bool cond2) {
  // hot path
  if (cond1) {
    /* cold code (1): 50 lines of error handling */
  }
  // hot path
  if (cond2) {
    /* cold code (2): 30 lines of logging */
  }
}
```

```cpp
// After: cold code outlined into separate functions
void foo(bool cond1, bool cond2) {
  // hot path
  if (cond1) { cold1(); }
  // hot path
  if (cond2) { cold2(); }
}

__attribute__((noinline)) void cold1() {
  /* cold code (1): 50 lines of error handling */
}
__attribute__((noinline)) void cold2() {
  /* cold code (2): 30 lines of logging */
}
```

**Key implementation details:**

1. **Use `__attribute__((noinline))`** on the outlined functions. Without this, the compiler may re-inline them, undoing the transformation.

2. **Alternatively, use `[[unlikely]]`** on the branch conditions instead of manually outlining:
   ```cpp
   if (cond1) [[unlikely]] {
       /* cold code -- compiler will avoid inlining and may place out of line */
   }
   ```
   This conveys to the compiler that inlining the cold path is not desired.

3. **Place outlined functions in `.text.cold`**: Use `__attribute__((section(".text.cold")))` or rely on the compiler/linker to place cold functions in a separate section. This improves memory footprint because the cold code won't be loaded into memory at runtime if it is never called.

### Automated: let PGO or BOLT handle it

- **PGO**: With profile data, the compiler can automatically identify cold blocks and outline them.
- **BOLT**: The `-split-functions` and `-split-all-cold` flags automatically split cold blocks out of hot functions based on runtime profile data.

## Expected Impact

- **I-cache density**: After splitting, the hot path of `foo` is compact. The `CALL cold1` instruction occupies only 5 bytes (x86) instead of the 50+ bytes of inlined cold code. Neighboring hot instructions now share cache lines with other hot instructions instead of cold code.
- **uop-cache**: The DSB caches decoded micro-ops based on code layout. Compact hot code means more hot uops fit in the DSB.
- **Typical speedup**: 2-5% for functions with complex CFG and large cold blocks between hot parts. Larger gains when the function is called frequently and the cold code is substantial.
- **Best candidates**: Functions with complex control flow graphs, many error-handling branches, and large blocks of cold code (> 20 instructions) between hot sections.

## Caveats

- **Do not outline hot code**: If the "cold" code is actually executed frequently, outlining it adds a function call overhead (call + return) on the hot path. Profile first to confirm the code is truly cold.
- **Function call overhead**: Each outlined cold function adds a `CALL` + `RET` pair (~5 cycles minimum). This is negligible if the cold path is rarely taken, but adds up if it is taken more often than expected.
- **Parameter passing**: If the cold code needs many local variables from the hot function, outlining requires passing them as parameters. This can add register pressure and stack spills. Consider passing a pointer to a struct or using a lambda capturing by reference.
- **Debugging complexity**: Outlined functions may make stack traces less intuitive. Use meaningful names for cold functions (e.g., `foo_handle_error`, `foo_log_diagnostics`).
- **Compiler may already do this**: With PGO enabled, the compiler performs function splitting automatically. Manual outlining is most useful when PGO is not available.
