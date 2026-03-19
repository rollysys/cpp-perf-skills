---
name: Compiler Optimization Reports — Free Performance Diagnostics
source: perf-book Ch.5, Ch.9
layers: [system]
platforms: [arm, x86]
keywords: [compiler report, optimization report, Rpass, fopt-info, vectorization missed, inline, diagnostic]
---

## Problem

Developers write code, add `-O2`, and hope the compiler does the right thing. When performance is disappointing, they jump to profiling or manual assembly — skipping the cheapest diagnostic available: **asking the compiler what it did**.

Compiler optimization reports tell you:
- Which loops were vectorized, and to what width
- Which loops were NOT vectorized, and exactly why (aliasing? data dependency? function call?)
- Which functions were inlined, and which were not (too large? recursive? indirect call?)
- Which loops were unrolled, and by what factor

This costs zero runtime, takes 5 seconds to enable, and should be the first thing checked for any hot function.

## Detection

**When to use optimization reports:**
- A loop you expected to vectorize is slower than expected
- A function you expected to be inlined shows up as a `bl`/`call` in the assembly
- Performance is inexplicably bad despite clean code
- Before and after any source-level optimization, to verify the compiler cooperated

## Transformation

### GCC Optimization Reports

```bash
# Vectorization: what was vectorized and what was missed
gcc -O2 -fopt-info-vec-optimized -c hot_module.cpp
# Output: hot_module.cpp:42:3: optimized: loop vectorized using 16 byte vectors

gcc -O2 -fopt-info-vec-missed -c hot_module.cpp
# Output: hot_module.cpp:58:3: missed: couldn't vectorize loop
# Output: hot_module.cpp:58:3: missed: not vectorized: unsupported data-type

# Inlining decisions
gcc -O2 -fopt-info-inline -c hot_module.cpp
# Output: hot_module.cpp:12:3: optimized: inlined 'helper' into 'hot_func'

# All optimization info at once (verbose)
gcc -O2 -fopt-info-all -c hot_module.cpp

# Write report to file instead of stderr
gcc -O2 -fopt-info-vec-missed=vec_report.txt -c hot_module.cpp

# Loop unrolling info
gcc -O2 -fopt-info-loop-optimized -c hot_module.cpp
```

### Clang Optimization Reports

```bash
# Vectorization: successful passes
clang -O2 -Rpass=loop-vectorize -c hot_module.cpp
# Output: hot_module.cpp:42:3: remark: vectorized loop (vectorization width: 4, ...)

# Vectorization: missed opportunities (most useful!)
clang -O2 -Rpass-missed=loop-vectorize -c hot_module.cpp
# Output: hot_module.cpp:58:3: remark: loop not vectorized: call instruction cannot be vectorized

# Vectorization: detailed analysis (why it failed)
clang -O2 -Rpass-analysis=loop-vectorize -c hot_module.cpp
# Output: hot_module.cpp:58:3: remark: loop not vectorized: value could not be identified as reduction

# All three together (recommended for investigating a hot function)
clang -O2 \
  -Rpass=loop-vectorize \
  -Rpass-missed=loop-vectorize \
  -Rpass-analysis=loop-vectorize \
  -c hot_module.cpp

# Inlining decisions
clang -O2 -Rpass=inline -Rpass-missed=inline -c hot_module.cpp

# Loop unrolling
clang -O2 -Rpass=loop-unroll -Rpass-missed=loop-unroll -c hot_module.cpp

# Save full optimization record to YAML (for tooling)
clang -O2 -fsave-optimization-record -c hot_module.cpp
# Creates hot_module.opt.yaml — parseable by opt-viewer.py
```

### MSVC Optimization Reports

```cmd
:: Vectorization report
cl /O2 /Qvec-report:2 hot_module.cpp
:: Output: hot_module.cpp(42): info C5001: loop vectorized
:: Output: hot_module.cpp(58): info C5002: loop not vectorized due to reason '1200'

:: Inline report
cl /O2 /Ob2 /d1reportSingleClassLayoutFoo hot_module.cpp
```

### Interpreting Common Messages

**Vectorization blockers (most common):**

| Message | Root Cause | Fix |
|---------|-----------|-----|
| "loop not vectorized: unsafe dependent memory operations" | Pointer aliasing | Add `__restrict__` to pointer params |
| "call instruction cannot be vectorized" | Function call in loop body | Inline the function or use SIMD intrinsics |
| "value could not be identified as reduction" | Complex reduction pattern | Simplify to `sum += expr` form |
| "loop not vectorized: loop contains a switch statement" | Control flow divergence | Convert to branchless (cmov, lookup table) |
| "not vectorized: unsupported data-type" | bool, char, or bitfield operations | Widen to int or float |
| "loop not vectorized: cannot prove it is safe to reorder" | FP associativity | Add `-ffast-math` or `#pragma clang loop vectorize(enable)` |
| "loop not vectorized: low trip count" | Loop runs < vector width iterations | Unroll manually or hint: `#pragma clang loop vectorize_width(N)` |

**Inlining blockers:**

| Message | Root Cause | Fix |
|---------|-----------|-----|
| "not inlined because too costly" | Function body too large | Split cold path into separate function |
| "not inlined because recursive" | Direct/indirect recursion | Convert to iterative |
| "not inlined because of indirect call" | Virtual method or function pointer | Devirtualize or use CRTP |

### Workflow: Use Reports in CI

```makefile
# Add to Makefile for hot modules
CXXFLAGS_DIAG = -Rpass-missed=loop-vectorize -Rpass-missed=inline

hot_module.o: hot_module.cpp
	$(CXX) $(CXXFLAGS) $(CXXFLAGS_DIAG) -c $< -o $@ 2> $@.opt-report
	@grep -c "not vectorized" $@.opt-report && echo "WARNING: vectorization regressions" || true
```

### Example: Fixing a vectorization failure

```cpp
// Compiler reports: "loop not vectorized: unsafe dependent memory operations"
void scale(float* output, const float* input, float factor, int n) {
    for (int i = 0; i < n; i++)
        output[i] = input[i] * factor;  // compiler can't prove output != input
}

// Fix: add __restrict__
void scale(float* __restrict__ output, const float* __restrict__ input,
           float factor, int n) {
    for (int i = 0; i < n; i++)
        output[i] = input[i] * factor;  // now vectorized
}
// Compiler reports: "loop vectorized (vectorization width: 4, interleaved count: 2)"
```

## Expected Impact

- **Time cost:** < 1 minute to add flags, < 5 minutes to read the report for a single file
- **Finding vectorization failures:** identifies the exact line and reason, saving hours of assembly inspection
- **Catching regressions:** adding optimization reports to CI detects when a code change breaks vectorization or inlining of a previously optimized function
- **No runtime overhead:** these are compile-time diagnostics, the generated binary is identical with or without reporting flags

## Caveats

- **Report verbosity:** on large translation units, optimization reports produce thousands of lines. Filter to the function of interest by file:line or pipe through grep.
- **Clang's `-fsave-optimization-record`** produces YAML files that can be viewed with `opt-viewer.py` (in the LLVM source tree) for an HTML report. Useful for large codebases.
- **"Loop vectorized" does not mean "loop is fast":** the compiler may vectorize at width 2 when width 8 is possible. Check the reported vectorization width and interleave count.
- **GCC and Clang report different things:** GCC's `-fopt-info` is less detailed than Clang's `-Rpass-analysis`. On GCC, you may need to inspect the assembly to understand why vectorization failed.
- **MSVC's error codes** are documented in the MSVC documentation but are less human-readable than GCC/Clang messages.
- **LTO changes the picture:** with LTO enabled, inlining decisions are made at link time. Compile-time reports may show "not inlined" for functions that are inlined during LTO. Check the LTO-phase report separately.
