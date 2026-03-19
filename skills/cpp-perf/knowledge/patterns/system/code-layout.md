---
name: Machine Code Layout Optimization
source: perf-book Ch.11
layers: [system, microarchitecture]
platforms: [arm, x86]
keywords: [code layout, BOLT, function splitting, basic block, hot path, cold path, PGO, icache, iTLB, branch target alignment, function reordering, basic block placement, LTO, AutoFDO, Propeller, HFSort, code footprint, huge pages]
---

## Problem

Machine code layout -- the physical arrangement of instructions in the binary -- directly impacts CPU Frontend efficiency. Poor layout causes:

1. **I-cache thrashing**: Hot code scattered across many cache lines forces frequent evictions. Functions and basic blocks that execute together but are placed far apart in memory waste cache capacity on cold code between them.

2. **ITLB misses**: When hot code is spread across many 4KB pages, the instruction TLB cannot cover the working set. For large binaries (e.g., Clang at ~60MB `.text` section), ITLB overhead can reach 7% of cycles just doing page walks.

3. **Fetch bandwidth waste**: The CPU Frontend fetches contiguous aligned blocks (16, 32, or 64 bytes depending on architecture). Every taken branch wastes the remaining bytes in the fetch block after the jump instruction and before the branch target. This reduces effective fetch throughput.

4. **Cache line straddling**: Hot loops that span multiple cache lines require the processor to fetch from two cache lines per iteration, even when the loop body is tiny enough to fit in one.

5. **Cold code pollution**: Error-handling, logging, and rarely-executed paths interleaved with hot code inflate the instruction footprint, displacing useful hot code from I-cache and uop-cache (DSB).

Typical symptoms by application class:
- **Large codebases** (databases, compilers, web browsers, cloud services): Millions of lines of code with the hot working set exceeding L1 I-cache (32KB). Clang17 compilation shows 52.3% Frontend Bound, with 5MB of non-cold code spread over 6614 4KB pages at only 19% page utilization.
- **Medium applications** with indirect jumps and complex call graphs: Stockfish (238KB `.text`) still hits 25.8% Frontend Bound due to many indirect jumps and function calls despite small code size.
- **Even small loops**: Alignment effects are measurable in microbenchmarks when a loop straddles a cache line boundary.

## Detection

### TMA (Top-down Microarchitecture Analysis) Metrics

- **Frontend Bound > 20%**: Worth investing time in layout optimization. Below 10% is the norm for well-optimized code.
- Drill into sub-categories: `ICache_Misses`, `ITLB_Misses`, `DSB_Coverage`.

### Hardware Performance Counters

On x86 (via `perf stat` or equivalent):
```
L1-icache-load-misses          # I-cache misses
iTLB-load-misses               # ITLB misses
frontend_retired.dsb_miss      # uop-cache misses (Intel)
```

On ARM (via PMU events):
```
L1I_CACHE_REFILL               # L1 instruction cache refill
L1I_TLB_REFILL                 # L1 instruction TLB refill
INST_RETIRED                   # for context on miss rates
```

### Code Footprint Measurement

- **perf-tools** (`perf-tools/do.py profile --profile-mask 100 -a <benchmark>`): Uses Intel LBR to estimate non-cold code footprint in KB and 4KB-pages. Key metric: page utilization = `footprint_KB / (pages * 4)`. Low utilization (e.g., 14-19%) indicates sparse hot code layout.
- **llvm-bolt-heatmap**: Produces visual code heatmaps showing hot/cold distribution in the binary. Useful for evaluating original layout and confirming optimized layout is more compact.
- **`readelf`**: Check `.text` section size as upper bound for code footprint.

### Quick Heuristics

- Binary `.text` section > 1MB: likely benefits from layout optimization.
- Non-cold code footprint exceeds L1 I-cache size (typically 32KB): I-cache pressure exists.
- Non-cold code spread over many 4KB pages (> 256 pages = 1MB): ITLB pressure likely.
- Many indirect jumps and calls: layout sensitivity is high even with smaller code.

## Transformation

### 1. Basic Block Placement (Compiler)

**Goal**: Keep hot code fall-through, push cold code out of line.

**Technique**: Invert branch conditions so the hot path is the fall-through path, and cold code (error handling, unlikely paths) requires a taken branch.

**Source-level hints** (C++20):
```cpp
if (error_condition) [[unlikely]] {
    handleError();
}
// hot path continues as fall-through
```

**Pre-C++20**:
```cpp
#define LIKELY(EXPR)   __builtin_expect((bool)(EXPR), true)
#define UNLIKELY(EXPR) __builtin_expect((bool)(EXPR), false)

if (UNLIKELY(error_condition)) {
    handleError();
}
```

**Switch statements** (C++20):
```cpp
switch (instruction) {
    [[likely]] case ADD: handleADD(); break;
               case NOP: handleNOP(); break;
               case RET: handleRET(); break;
}
```

**Why it helps**:
- Not-taken branches are cheaper than taken branches (Intel Skylake: 2 untaken/cycle vs 1 taken/2 cycles).
- Contiguous hot code eliminates fetch block waste and improves I-cache and uop-cache utilization.

### 2. Basic Block Alignment (Compiler)

**Goal**: Prevent hot loops from straddling cache line boundaries.

**Fine-grained control** (Clang):
```cpp
[[clang::code_align(64)]]
for (int i = 0; i < N; ++i) {
    // hot loop body
}
```

**Inline assembly fallback**:
```cpp
asm(".align 64;");
for (int i = 0; i < N; ++i) { ... }
```

**Compiler flags** (use with caution -- affects entire translation unit):
- `-mllvm -align-all-blocks=5` (align all blocks to 32B, LLVM)
- LLVM default: loops aligned to 16B boundaries.

**Caveat**: Alignment NOPs increase code size. Only use targeted alignment on performance-critical loops.

### 3. Function Splitting / Outlining (Compiler)

**Goal**: Separate hot code from cold code within a function.

**Manual transformation**:
```cpp
// Before: cold code inline
void foo(bool cond1, bool cond2) {
    // hot path
    if (cond1) { /* large cold block */ }
    // hot path
    if (cond2) { /* large cold block */ }
}

// After: cold code outlined
void foo(bool cond1, bool cond2) {
    if (cond1) cold1();
    if (cond2) cold2();
}
void cold1() __attribute__((noinline)) { /* cold code */ }
void cold2() __attribute__((noinline)) { /* cold code */ }
```

**Key details**:
- Use `__attribute__((noinline))` or `[[unlikely]]` on the branch to prevent the compiler from re-inlining outlined functions.
- Place outlined functions in `.text.cold` segment so they are not loaded into memory if never called.
- Best for functions with complex CFG and large cold blocks between hot paths.

### 4. Function Reordering (Linker)

**Goal**: Group hot functions together to share cache lines and reduce code footprint.

**Compiler flag**: `-ffunction-sections` (places each function in its own section, enabling reordering).

**Linker options**:
- Gold linker: `--section-ordering-file=order.txt`
- LLD linker: `--symbol-ordering-file=order.txt`
- GNU linker: Use linker scripts.

**Automated tools**:
- **HFSort** (Meta): Generates section ordering file from profiling data. Observed 2% speedup on large distributed cloud applications (Facebook, Baidu, Wikipedia). Integrated into HHVM, LLVM BOLT, and LLD.
- **HFSort+** and **CDSort (Cache-Directed Sort)**: Successors to HFSort with further improvements for large code footprint workloads.

### 5. Profile-Guided Optimization -- PGO (Compiler)

**Goal**: Use runtime profiling data to drive all layout decisions.

#### Instrumented PGO

Three-step workflow:
1. **Instrument**: Compile with profiling instrumentation.
2. **Train**: Run instrumented binary with representative workload to collect profile data.
3. **Optimize**: Recompile with profile data.

**Clang/LLVM**:
```bash
# Step 1: Instrument
clang++ -fprofile-instr-generate -o app_instrumented app.cpp
# Step 2: Collect profile
./app_instrumented <representative_workload>
llvm-profdata merge -output=app.profdata default.profraw
# Step 3: Optimize
clang++ -fprofile-instr-use=app.profdata -o app_optimized app.cpp
```

**GCC**:
```bash
# Step 1: Instrument
g++ -fprofile-generate -o app_instrumented app.cpp
# Step 2: Collect profile
./app_instrumented <representative_workload>
# Step 3: Optimize
g++ -fprofile-use -o app_optimized app.cpp
```

**What PGO improves**: Function inlining decisions, code placement, register allocation, basic block ordering.

**Drawback**: Instrumented binary incurs 5-10x runtime overhead. Profile data becomes stale as source code evolves and must be recollected.

#### Sample-based PGO (AutoFDO)

Uses Linux `perf` sampling data instead of instrumentation -- no special build step, much lower overhead, can collect from production.

**Tool**: [AutoFDO](https://github.com/google/autofdo) (Google) -- converts `perf` sampling data to compiler-consumable format.

**Advantages over instrumented PGO**:
- No instrumented build required.
- Low runtime overhead -- can profile in production.
- Enables hardware-telemetry-driven optimizations (e.g., branch-to-cmov conversion based on misprediction rates, available on Intel Skylake+).

### 6. Post-Link Binary Optimization (BOLT, Propeller)

**Goal**: Optimize machine code layout after linking, using runtime profile data.

#### BOLT (Meta / LLVM)

```bash
# Collect profile with perf
perf record -e cycles:u -j any,u -o perf.data -- ./app <workload>
# Convert perf data
perf2bolt -p perf.data -o perf.fdata ./app
# Optimize binary
llvm-bolt ./app -o ./app.bolt -data=perf.fdata -reorder-blocks=ext-tsp \
    -reorder-functions=hfsort -split-functions -split-all-cold \
    -dyno-stats -hugify
```

**BOLT optimizations** (15+ passes):
- Basic block reordering (`-reorder-blocks`)
- Function splitting (`-split-functions`, `-split-all-cold`)
- Function reordering (`-reorder-functions=hfsort`)
- Huge page mapping for hot code (`-hugify`)

**BOLT is part of LLVM since January 2022.**

#### Propeller (Google)

Similar purpose to BOLT but works with linker input rather than disassembling the binary. Can be distributed across machines for better scaling and lower memory consumption.

**Both BOLT and Propeller can be used on top of PGO + LTO for additional gains.**

### 7. Reducing ITLB Misses with Huge Pages

**Goal**: Map hot code onto 2MB huge pages to reduce ITLB misses.

**Method 1 -- Relink with alignment**:
```bash
# Link with 2MB page alignment
clang++ -Wl,-zcommon-page-size=2097152 -Wl,-zmax-page-size=2097152 -o app app.o
# Set ELF header for huge page loading
hugeedit --text /path/to/app
```

**Method 2 -- Runtime remapping** (no recompilation needed):
```bash
# Using iodlr library (Intel, Linux)
LD_PRELOAD=/usr/lib64/liblppreload.so ./app
```

**Method 3 -- BOLT `-hugify`**: Automatically maps only hot code to 2MB pages using Linux THP, minimizing page fragmentation.

**Impact**: Reduces ITLB misses by up to 50%, yielding up to 10% speedup for large applications.

## Expected Impact

| Technique | Typical Speedup | Best Candidates |
|-----------|----------------|-----------------|
| Basic block placement (`[[likely]]`/`[[unlikely]]`) | 1-5% | Code with many branches, error-handling paths |
| Basic block alignment | 0-5% (variable, noisy) | Small hot loops straddling cache lines |
| Function splitting | 2-5% | Functions with complex CFG, large cold blocks |
| Function reordering (HFSort/CDSort) | ~2% | Many small hot functions, large binaries |
| Instrumented PGO | 5-30% | Large codebases with severe Frontend bottleneck |
| Sample-based PGO (AutoFDO) | 5-15% | Production workloads where instrumentation overhead is unacceptable |
| BOLT / Propeller (on top of PGO) | Additional 5-10% | Large binaries already using PGO |
| Huge pages for code | Up to 10% | Applications with `.text` > 1MB and high ITLB miss rate |
| **Combined (LTO + PGO + BOLT + huge pages)** | **15-30%+** | **Large applications with 20%+ Frontend Bound** |

Real-world data points from the book:
- Clang17 compilation: 52.3% Frontend Bound, 5MB non-cold code footprint -- primary candidate for PGO/BOLT.
- Blender: 29.4% Frontend Bound, but only 313KB of non-cold code out of 133MB `.text` -- less benefit expected.
- Stockfish: 25.8% Frontend Bound from indirect jumps/calls despite 238KB `.text` -- benefits from call graph optimization.
- Meta cloud services: 2% from HFSort function reordering alone.

## Caveats

1. **Small binaries / compute-bound code**: When the entire hot code fits in L1 I-cache (32KB) and the workload is dominated by backend execution (ALU, memory latency), layout optimizations provide negligible benefit. CloverLeaf (104KB non-cold, 5.3% Frontend Bound) is an example.

2. **PGO training data representativeness**: The compiler "blindly" uses profile data. Training on unrepresentative workloads can degrade performance for real use cases. Profile data from different workloads can be merged to mitigate this, but care is still required.

3. **Profile staleness**: As source code evolves, profile data becomes stale and must be recollected. This is a significant operational burden for instrumented PGO (5-10x slowdown during collection).

4. **Alignment is noisy**: Machine code layout is a major source of measurement noise. Changes in alignment can cause performance swings of several percent that are unrelated to the actual optimization. This makes it harder to distinguish real improvements from accidental layout effects.

5. **Code size bloat from alignment**: Aggressive alignment (e.g., `-mllvm -align-all-blocks=5`) inserts NOP padding everywhere, increasing code size and potentially harming I-cache utilization. Use targeted alignment (`[[clang::code_align()]]`) only on critical loops.

6. **Huge page overhead for small programs**: Programs with code sections of only a few KB waste memory when mapped to 2MB huge pages. Regular 4KB pages are more memory-efficient for small applications.

7. **Shared library limitation**: Functions in dynamically linked shared libraries do not participate in the careful layout of machine code in the main binary. Layout optimization is most effective for statically linked code or within a single shared library.

8. **Platform-specific tooling**: Some tools are platform-limited -- `perf-tools` code footprint measurement requires Intel LBR (no AMD/ARM support); `hugeedit`/`iodlr` are Linux-only; BOLT requires Linux ELF binaries.

9. **LTO interaction**: Link-Time Optimization (LTO/IPO) can reduce hot region size and enable better cross-module inlining. It is complementary to PGO and BOLT -- use all three together for maximum benefit.
