---
name: Replace Branches with Conditional Moves
source: perf-ninja bad_speculation/branches_to_cmov_1, perf-book Ch.10
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [branch, cmov, csel, predication, branch misprediction, conditional, ternary, branchless]
---

## Problem

When a branch condition depends on data that is effectively random or has no stable pattern, the CPU branch predictor cannot predict the outcome reliably. Each misprediction flushes the pipeline and costs 15-20 cycles on modern x86 and 10-15 cycles on ARM cores. If the bodies of both paths are small (a handful of instructions), the total work of executing both paths unconditionally is cheaper than the recurring misprediction penalty.

Common trigger: an `if/else` or `switch` that selects between two small values based on data-dependent conditions in a hot loop, where the condition has near-50/50 or otherwise unpredictable distribution.

**perf-ninja example -- Game of Life rules:**

The `simulateNext()` method uses a `switch(aliveNeighbours)` to decide the next cell state. With a 1024x1024 grid and ~30% alive cells, the neighbour count distribution is spread across cases 0-8, making branch prediction unreliable.

```cpp
// Original: switch with multiple branches
switch(aliveNeighbours) {
    case 0:
    case 1:  future[i][j] = 0;               break;  // dies of loneliness
    case 2:  future[i][j] = current[i][j];    break;  // stays the same
    case 3:  future[i][j] = 1;                break;  // birth
    default: future[i][j] = 0;                        // overpopulation
}
```

## Detection

**Source-level indicators:**
- `if/else` chains or `switch` statements inside hot loops
- Conditions depend on runtime data (array values, user input, random-like distributions)
- Both branches assign to the same variable with small computations (no side effects, no function calls)

**Profile-level indicators:**
- TMA: high `Bad_Speculation > Branch_Mispredict` metric (> 10% of pipeline slots)
- `perf stat`: `branch-misses` / `branches` ratio > 5% on the hot function
- `perf record`: misprediction events concentrated on specific branch instructions

**Disassembly clues:**
- `je`/`jne`/`jl`/`jg` instructions at high-IPC bottleneck locations
- No `cmov` or `csel` generated despite small, side-effect-free branch bodies
- ARM: absence of `csel`/`csinc`/`csneg` instructions in conditional code

## Transformation

### Strategy 1: Ternary operator to encourage CMOV/CSEL

Replace `if/else` with a ternary expression. For data-dependent branches with small payloads, compilers are more likely to emit conditional move instructions for ternary expressions.

```cpp
// Before: if/else with branch
int a;
if (cond) {       // frequently mispredicted
    a = computeX();
} else {
    a = computeY();
}
foo(a);
```

```cpp
// After: ternary -- compiler generates CMOV (x86) or CSEL (ARM)
int x = computeX();
int y = computeY();
int a = cond ? x : y;
foo(a);
```

x86 assembly for the branchless version:
```asm
call <computeX>      # compute x
mov  ebp, eax        # save x
call <computeY>      # compute y
test ebx, ebx        # test cond
cmovne eax, ebp      # a = cond ? x : y (no branch)
```

ARM assembly equivalent uses `csel`:
```asm
bl   computeX        // compute x
mov  w19, w0         // save x
bl   computeY        // compute y
cmp  w20, #0         // test cond
csel w0, w19, w0, ne // a = cond ? x : y (no branch)
```

### Strategy 2: Arithmetic replacement for Game of Life rules

Replace the `switch` with branchless arithmetic using a lookup array or direct computation:

```cpp
// Before: switch with 4+ branches per cell
switch(aliveNeighbours) {
    case 0:
    case 1:  future[i][j] = 0;               break;
    case 2:  future[i][j] = current[i][j];    break;
    case 3:  future[i][j] = 1;                break;
    default: future[i][j] = 0;
}
```

```cpp
// After: branchless using lookup table indexed by neighbour count
// Rules: alive if (neighbours == 3) OR (neighbours == 2 AND currently alive)
static constexpr int lut_dead[9]  = {0,0,0,1,0,0,0,0,0};
static constexpr int lut_alive[9] = {0,0,1,1,0,0,0,0,0};
future[i][j] = current[i][j] ? lut_alive[aliveNeighbours]
                              : lut_dead[aliveNeighbours];
```

Or purely arithmetic:
```cpp
// After: branchless arithmetic
// alive iff (n == 3) || (n == 2 && cell == 1)
future[i][j] = (aliveNeighbours == 3) | (aliveNeighbours == 2 & current[i][j]);
```

### Strategy 3: Use `__builtin_unpredictable` hint (Clang 17+)

When you cannot restructure the code easily, hint to the compiler that a branch is unpredictable:

```cpp
if (__builtin_unpredictable(cond)) {
    a = computeX();
} else {
    a = computeY();
}
```

This encourages the compiler to emit `cmov`/`csel` instead of a branch, but does not guarantee it.

## Expected Impact

- **Branch misprediction cost:** 15-20 cycles per mispredict on modern x86, 10-15 cycles on ARM Cortex-A/Neoverse.
- **Elimination benefit:** For a loop with 50% misprediction rate, removing the branch saves ~8-10 cycles per iteration on average.
- **perf-ninja branches_to_cmov_1:** The Game of Life switch is exercised ~1M times per grid (1024x1024). With ~30% misprediction, eliminating branches can yield 2-4x speedup in the `simulateNext()` function.
- **Typical range:** 1.5-5x speedup for loops dominated by mispredicted branches on random data.

## Caveats

- **Do NOT apply to well-predicted branches.** If the branch predictor achieves > 95% accuracy (e.g., loop exit conditions, sorted data), a branch is faster because the CPU can speculate correctly and run ahead. CMOV introduces a data dependency that prevents speculation.
- **Both sides must be cheap.** If `computeX()` or `computeY()` is expensive (> 20 instructions, memory allocation, I/O), executing both unconditionally wastes more work than the misprediction penalty saves.
- **No side effects.** Both paths must be free of side effects (no writes to shared state, no exceptions). CMOV executes both sides unconditionally.
- **Compiler resistance.** Compilers may refuse to generate CMOV even for simple ternaries. Check the disassembly. Use `__builtin_unpredictable` (Clang 17+) or consider inline assembly as a last resort.
- **Floating-point:** For FP conditionals, use `FCMOVcc` (x86, legacy) or `VMAXSS`/`VMINSS` for min/max patterns. ARM uses `FCSEL`.
- **CMOV has data dependency:** Converting control flow to data flow means the CPU cannot speculate past the CMOV. In some cases this can stall the pipeline even without mispredictions.
