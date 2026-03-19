---
name: Profile-Guided and Link-Time Optimization (PGO + LTO)
source: perf-book Ch.11, Section 11-7 PGO
layers: [system, microarchitecture]
platforms: [arm, x86]
keywords: [PGO, LTO, profile guided, link time optimization, fprofile-generate, fprofile-use, fprofile-instr-generate, fprofile-instr-use, IPO, AutoFDO, BOLT, Propeller, FDO, instrumented PGO, sample-based PGO, binary optimization]
---

## Problem

Without runtime profiling data, the compiler must guess at branch probabilities, function call frequencies, and hot/cold code distribution. These guesses drive critical decisions: which functions to inline, how to order basic blocks, whether to unroll loops, and how to allocate registers. For large codebases (millions of lines), incorrect guesses lead to severe Frontend bottlenecks.

Real-world data from the book:
- Clang17 compilation: **52.3% Frontend Bound**, with 5MB of non-cold code spread over 6614 4KB pages at only 19% page utilization
- Workloads with severe Frontend bottlenecks can see **up to 30% speedup** from PGO
- Compute-bound workloads (scientific computing) may see no benefit at all

PGO is the only practical way to optimize machine code layout for applications with millions of lines of code. Manual annotation of every branch with `[[likely]]`/`[[unlikely]]` is not feasible at scale.

## Detection

**When to use PGO/LTO:**
- TMA: `Frontend_Bound` > 20% (worth investing time; below 10% is the norm)
- Large `.text` section (> 1MB) with non-cold code footprint exceeding L1 I-cache (32KB)
- Application has representative workloads that can be used for training
- Code with many branches, indirect calls, or virtual dispatch

**When PGO is NOT worth the effort:**
- Small, compute-bound programs (scientific kernels, SIMD-heavy code)
- Programs where `Frontend_Bound` < 10%
- No representative training workload available (risk of pessimizing real use cases)

## Transformation

### Instrumented PGO (Clang/LLVM)

Three-step workflow:

```bash
# Step 1: Build with instrumentation
clang++ -fprofile-instr-generate -o app_instrumented app.cpp

# Step 2: Run with representative workload to collect profile
./app_instrumented <representative_workload>
# Produces default.profraw
llvm-profdata merge -output=app.profdata default.profraw

# Step 3: Rebuild with profile data
clang++ -fprofile-instr-use=app.profdata -o app_optimized app.cpp
```

### Instrumented PGO (GCC)

```bash
# Step 1: Instrument
g++ -fprofile-generate -o app_instrumented app.cpp

# Step 2: Collect profile
./app_instrumented <representative_workload>
# Produces .gcda files

# Step 3: Optimize
g++ -fprofile-use -o app_optimized app.cpp
```

### Sample-based PGO (AutoFDO)

Eliminates the instrumented build step. Uses Linux `perf` sampling data instead:

```bash
# Step 1: Build normally (optimized)
clang++ -O2 -o app app.cpp

# Step 2: Collect samples with perf
perf record -e cycles:u -b -o perf.data -- ./app <workload>

# Step 3: Convert with AutoFDO
create_llvm_prof --binary=app --profile=perf.data --out=app.afdo

# Step 4: Rebuild with sample profile
clang++ -O2 -fprofile-sample-use=app.afdo -o app_optimized app.cpp
```

**Advantages over instrumented PGO:**
- No instrumented binary needed (skip step 1)
- Much lower runtime overhead -- can profile in production
- Enables hardware-telemetry optimizations (e.g., branch-to-cmov conversion based on actual misprediction rates, available on Intel Skylake+)

### Link-Time Optimization (LTO)

LTO enables cross-translation-unit optimizations: inlining across object files, whole-program dead code elimination, and better code layout.

```bash
# Clang/LLVM ThinLTO (scalable, recommended for large projects)
clang++ -flto=thin -O2 -o app app.cpp lib.cpp

# Clang/LLVM Full LTO (more aggressive, higher compile time)
clang++ -flto -O2 -o app app.cpp lib.cpp

# GCC LTO
g++ -flto -O2 -o app app.cpp lib.cpp
```

LTO reduces hot region size by enabling cross-module inlining (eliminating call overhead for small hot functions in other translation units) and whole-program dead code elimination.

### Post-Link Binary Optimization: BOLT

BOLT optimizes the binary after linking, using profile data from `perf`:

```bash
# Collect profile
perf record -e cycles:u -j any,u -o perf.data -- ./app <workload>

# Convert perf data to BOLT format
perf2bolt -p perf.data -o perf.fdata ./app

# Optimize binary (all major passes)
llvm-bolt ./app -o ./app.bolt -data=perf.fdata \
    -reorder-blocks=ext-tsp \
    -reorder-functions=hfsort \
    -split-functions -split-all-cold \
    -dyno-stats -hugify
```

BOLT has 15+ optimization passes including basic block reordering, function splitting and reordering, and huge page mapping for hot code. Part of LLVM since January 2022.

### Post-Link Binary Optimization: Propeller (Google)

Similar purpose to BOLT but relies on linker input rather than disassembling the binary. Can be distributed across machines for better scaling and lower memory consumption.

### Combined pipeline for maximum benefit

```bash
# 1. Instrumented PGO build
clang++ -fprofile-instr-generate -flto=thin -o app_instr app.cpp
./app_instr <workload>
llvm-profdata merge -output=app.profdata default.profraw

# 2. PGO + LTO optimized build
clang++ -fprofile-instr-use=app.profdata -flto=thin -O2 -o app_pgo app.cpp

# 3. BOLT post-link optimization on top
perf record -e cycles:u -j any,u -o perf.data -- ./app_pgo <workload>
perf2bolt -p perf.data -o perf.fdata ./app_pgo
llvm-bolt ./app_pgo -o ./app_final -data=perf.fdata \
    -reorder-blocks=ext-tsp -reorder-functions=hfsort \
    -split-functions -split-all-cold -hugify
```

## Expected Impact

| Technique | Typical Speedup | Best Candidates |
|-----------|----------------|-----------------|
| Instrumented PGO | 5-30% | Large codebases with severe Frontend bottleneck |
| Sample-based PGO (AutoFDO) | 5-15% | Production workloads where instrumentation overhead is unacceptable |
| LTO | 5-20% | Multi-TU projects with cross-module hot call sites |
| BOLT / Propeller (on top of PGO) | Additional 5-10% | Large binaries already using PGO |
| **Combined (LTO + PGO + BOLT)** | **15-30%+** | **Large applications with 20%+ Frontend Bound** |

What PGO specifically improves:
- Function inlining decisions (inline hot callees, don't inline cold ones)
- Basic block placement (hot path fall-through based on measured branch frequencies)
- Register allocation (allocate registers based on actual variable lifetimes)
- Code placement (group hot code, separate cold code)

## Caveats

1. **Instrumentation overhead**: Instrumented PGO binaries run 5-10x slower than normal. This makes profile collection slow and prevents profiling directly from production systems.

2. **Profile staleness**: As source code evolves, profile data becomes stale and must be recollected. This is a significant operational burden. AutoFDO mitigates this by enabling continuous profiling from production.

3. **Training data representativeness**: The compiler "blindly" uses the profile data provided. Training on unrepresentative workloads can degrade performance for real use cases. Profile data from multiple workloads can be merged, but care is still required.

4. **Build system complexity**: PGO adds two extra build steps (instrument + collect). LTO requires all object files at link time. BOLT adds a post-link step. This increases build times and CI complexity.

5. **Compute-bound code gets no benefit**: Workloads dominated by ALU operations, memory latency, or SIMD computation (not instruction fetch) see no improvement from layout optimization.

6. **BOLT requires Linux ELF binaries**: BOLT's binary rewriting currently works on Linux ELF format. Windows PE and macOS Mach-O are not supported or have limited support.

7. **LTO compile time**: Full LTO can dramatically increase link time for large projects. ThinLTO (Clang) or `-flto=auto` (GCC) provide a scalable alternative with most of the benefit.
