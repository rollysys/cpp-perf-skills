---
name: CCMP Fused Conditional Compare (Branch Reduction)
source: optimized-routines string/aarch64/strcmp.S, memcmp.S
layers: [microarchitecture]
platforms: [arm]
keywords: [ccmp, conditional compare, fused condition, branch reduction, strcmp, memcmp, ARM]
---

## Problem

Evaluating compound conditions (`if (a == b && c == d)`) typically compiles to two separate compare-and-branch instruction pairs. Each branch is an independent prediction point -- if either mispredicts, the pipeline flushes. For string comparison functions like `strcmp` and `memcmp`, the inner loop checks TWO conditions every iteration:

1. Are the bytes equal? (data comparison)
2. Is there a NUL terminator? (end-of-string check)

Two branches per iteration halves the effective branch prediction capacity and doubles the misprediction surface.

ARM's `CCMP` (Conditional Compare) instruction fuses two conditions into a single branch: it performs the second comparison ONLY if the first condition passed, setting the flags directly so a single `b.cc` branch can check both conditions.

## Detection

- Two consecutive compare-and-branch pairs testing related conditions:
  ```
  cmp  x0, x1
  b.ne .Lfail
  cmp  x2, x3
  b.ne .Lfail
  ```
- `if (cond1 && cond2)` patterns in hot loops, especially in string/memory operations
- `strcmp`/`memcmp`-style loops with dual exit conditions (mismatch OR end-of-buffer)
- Profile shows high branch misprediction rate on one of the two branches in a compound condition

## Transformation

### Basic CCMP pattern

```
// Before: two branches (2 prediction points)
cmp   x0, x1          // condition 1: are data words equal?
b.ne  .Lfail          // branch 1
cmp   x2, x3          // condition 2: are next data words equal?
b.ne  .Lfail          // branch 2

// After: one branch (1 prediction point)
cmp   x0, x1          // condition 1
ccmp  x2, x3, #0, eq  // IF condition 1 passed (eq), THEN compare x2 vs x3
                       // ELSE set flags to #0 (forces NE → will take the branch)
b.ne  .Lfail          // single branch covers both conditions
```

The `ccmp x2, x3, #0, eq` instruction means:
- If the current flags satisfy `eq` (previous cmp was equal): perform `cmp x2, x3` and set flags normally
- If the current flags do NOT satisfy `eq`: set the NZCV flags to the immediate value `#0` (which clears Z, meaning NE will be true)

### CCMP in strcmp (from optimized-routines strcmp.S)

ARM's optimized strcmp loads 8 bytes at a time and checks both "words equal" and "contains NUL":

```
// Simplified from optimized-routines strcmp.S
.Lloop:
    ldr   x2, [x0], #8         // load 8 bytes from string 1
    ldr   x3, [x1], #8         // load 8 bytes from string 2

    // Check for NUL in x2 (using subtraction trick to detect zero bytes)
    sub   x4, x2, x5           // x5 = 0x0101010101010101
    bic   x4, x4, x2           // magic zero-byte detection
    ands  x4, x4, x6           // x6 = 0x8080808080808080; sets Z if no NUL

    // CCMP: if no NUL found (Z set), ALSO check if words are equal
    ccmp  x2, x3, #0, eq       // if no NUL: compare words; if NUL: force NE
    b.eq  .Lloop                // continue ONLY if: no NUL AND words equal

    // Exit: either found NUL or found mismatch -- determine which and where
```

This reduces the loop body from 2 branches to 1, and the remaining branch (loop back-edge) is highly predictable (taken until near end of string).

### CCMP chaining: three or more conditions

CCMP can be chained to fuse arbitrarily many conditions into one branch:

```
// Three conditions fused into one branch:
// if (a == b && c == d && e == f)
cmp   x0, x1              // check a == b
ccmp  x2, x3, #0, eq      // if a==b, check c == d
ccmp  x4, x5, #0, eq      // if a==b && c==d, check e == f
b.ne  .Lfail               // single branch for all three conditions
```

### C code that encourages CCMP generation

Compilers (GCC 7+, Clang 5+) can generate CCMP automatically from idiomatic C:

```cpp
// Compiler generates CCMP for this pattern (with -O2)
while (pos < len && data[pos] == target[pos]) {
    ++pos;
}

// Also works for compound boolean expressions:
if (x == expected_x && y == expected_y && z == expected_z) {
    // fast path
}

// Explicit hint for reluctant compilers (GCC):
// Use __builtin_expect to hint that the compound condition is usually true
if (__builtin_expect(a == b && c == d, 1)) {
    // hot path
}
```

### CCMP with different condition codes

```
// if (a < b || c > d) -- OR conditions use the inverse
// De Morgan: !(a >= b && c <= d)
cmp   x0, x1
ccmp  x2, x3, #0b0010, ge   // if a >= b, check c <= d; else force NE
b.le  .Lno_match             // both conditions false -> skip
// At least one condition true -> handle match

// if (a == b && c != 0) -- mixed conditions
cmp   x0, x1
ccmp  x2, #0, #0b0100, eq   // if a == b, check c != 0; else set Z (forces eq)
b.eq  .Lskip                 // skip if a != b OR c == 0
```

### CCMP vs CSEL: when to use which

| Pattern | Use CCMP | Use CSEL |
|---------|----------|----------|
| `if (A && B) goto label` | Yes -- fuse conditions into one branch | No |
| `x = (A && B) ? val1 : val2` | No | Yes -- conditional select |
| Loop with dual exit conditions | Yes -- CCMP in loop body | No |
| Branchless min/max | No | Yes -- `csel` |

## Expected Impact

| Scenario | Before (2 branches) | After (CCMP + 1 branch) | Improvement |
|----------|---------------------|-------------------------|-------------|
| strcmp inner loop | 2 branches/iter | 1 branch/iter | 50% fewer branches |
| memcmp 8B-at-a-time | 2 branches/iter | 1 branch/iter | 15-30% faster |
| Compound condition (3 checks) | 3 branches | 1 branch | 67% fewer branches |
| Branch misprediction surface | N prediction entries | 1 prediction entry | N-1 fewer mispredictions possible |

For strcmp-heavy workloads (JSON parsing, key lookup, configuration processing), CCMP reduces branch mispredictions by 30-50% in the comparison functions.

## Caveats

- **ARM-only instruction.** x86 does not have CCMP (x86 `CMOVcc` and `SETcc` serve different purposes). On x86, the compiler may use `test` + `and` + combined flags, or `cmov` chains for similar effect, but no direct equivalent.
- **Compilers usually generate CCMP automatically** from `&&` and `||` expressions with `-O2`. Check the disassembly before hand-writing assembly. Unnecessary inline assembly hurts readability and maintainability.
- **The immediate NZCV value is critical.** The 4-bit immediate (`#0`, `#0b0100`, etc.) sets the flags when the condition is false. Getting this wrong silently produces incorrect logic. For `&&` patterns with `b.ne`, use `#0` (clears Z, forces NE). For `||` patterns, the immediate must be the inverse.
- **CCMP does not improve prediction accuracy.** It reduces the NUMBER of branches, which reduces the total number of mispredictions. Each remaining branch still has the same per-branch prediction accuracy.
- **Deep CCMP chains (> 3) may not help** because the flags dependency creates a serial chain. The CMP -> CCMP -> CCMP latency is 1 cycle per link on most ARM cores, so 4+ chained CCMPs add 3+ cycles of latency before the branch can resolve. For very deep condition chains, consider reorganizing to check the most-likely-to-fail condition first.
