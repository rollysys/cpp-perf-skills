---
name: Function Reordering for I-cache and ITLB Locality
source: perf-book Ch.11, Section 11-6 Function Reordering
layers: [system, microarchitecture]
platforms: [arm, x86]
keywords: [function reordering, function grouping, HFSort, CDSort, cache-directed sort, I-cache, code footprint, linker, section ordering, ffunction-sections, hot function, call graph, LLD, Gold linker]
---

## Problem

When hot functions are scattered throughout the binary with cold functions placed between them, the CPU Frontend must fetch cache lines containing cold code just to execute a sequence of hot function calls. This inflates the code footprint -- the total number of cache lines the CPU needs to fetch -- and reduces I-cache utilization.

Example from the book: three hot functions `foo`, `bar`, and `zoo` with call pattern `foo -> zoo -> bar`. In the default layout, cold functions sit between them, so the sequence of two calls requires fetching four cache lines. By reordering so hot functions are adjacent and placed in call order, the same sequence fits in three cache lines, and `zoo`'s code is already in the I-cache when `foo` calls it.

```
Default layout:                         Improved layout:
[foo] [cold_a] [cold_b] [zoo] [cold_c] [bar]    [foo] [zoo] [bar] [cold_a] [cold_b] [cold_c]
  |                       ^       |               |      ^     ^
  +------call zoo---------+       |               +--call+     |
                                  |                      +call-+
         4 cache line reads                       3 cache line reads
```

This optimization works best when there are many small hot functions, which is common in large codebases with well-factored object-oriented or functional code.

## Detection

**Source-level indicators:**
- Large projects with hundreds or thousands of small functions
- Object-oriented code with many virtual method calls across classes
- Codebases where profiling shows many functions are hot but each is relatively small

**Profile-level indicators:**
- TMA: `Frontend_Bound` > 10% with `ICache_Misses` elevated
- Code footprint measurement shows low page utilization (e.g., 14-19% -- hot code is sparse across many pages)
- `llvm-bolt-heatmap` shows hot code scattered across the binary with cold gaps between

**Tooling for measurement:**
- **perf-tools**: `perf-tools/do.py profile --profile-mask 100 -a <benchmark>` (Intel LBR-based, estimates non-cold code footprint)
- **llvm-bolt-heatmap**: Visual heatmap of hot/cold code distribution in the binary
- **readelf**: `readelf -S <binary> | grep .text` for `.text` section size as upper bound

## Transformation

### Step 1: Compile with `-ffunction-sections`

This places each function in its own ELF section, enabling the linker to reorder them independently:

```bash
clang++ -ffunction-sections -c -o app.o app.cpp
```

### Step 2: Provide ordering to the linker

**LLD linker (LLVM):**
```bash
ld.lld --symbol-ordering-file=order.txt -o app app.o
```

**Gold linker:**
```bash
ld.gold --section-ordering-file=order.txt -o app app.o
```

**GNU linker:** Use linker scripts (more complex).

The `order.txt` file contains function names in the desired order, one per line, hot functions first and grouped by call affinity.

### Step 3: Generate ordering automatically with profiling tools

**HFSort** (Meta, integrated into BOLT and LLD):
Automatically generates the section ordering file from profiling data. Analyzes the call graph and groups functions that frequently call each other.

**CDSort (Cache-Directed Sort):**
The latest algorithm, superseding HFSort and HFSort+. Provides further improvements for workloads with large code footprints. Available in LLVM's `llvm/lib/Transforms/Utils/CodeLayout.cpp`.

**BOLT:**
```bash
# Collect profile
perf record -e cycles:u -j any,u -o perf.data -- ./app <workload>
perf2bolt -p perf.data -o perf.fdata ./app

# Optimize with function reordering
llvm-bolt ./app -o ./app.bolt -data=perf.fdata \
    -reorder-functions=hfsort -dyno-stats
```

### Fully automated approach with BOLT

BOLT handles function reordering as one of its 15+ optimization passes. It can be combined with basic block reordering and function splitting in a single invocation:

```bash
llvm-bolt ./app -o ./app.bolt -data=perf.fdata \
    -reorder-blocks=ext-tsp \
    -reorder-functions=hfsort \
    -split-functions -split-all-cold
```

## Expected Impact

- **Meta's production results**: HFSort function reordering alone yielded 2% speedup on large distributed cloud applications (Facebook, Baidu, Wikipedia).
- **Cache line savings**: Grouping N hot functions that were separated by cold code can reduce the number of cache lines fetched from ~2N to ~N (by eliminating cold code gaps).
- **Page utilization**: Improves the ratio of hot bytes per 4KB page. For example, Clang17 at 19% page utilization has substantial room for improvement.
- **Best candidates**: Applications with many small hot functions and large code footprints (`.text` > 1MB).

## Caveats

- **Requires profiling data**: Effective function reordering depends on knowing which functions are hot and their call relationships. Without profiling data, ordering is guesswork.
- **Shared libraries don't participate**: Functions in dynamically linked shared libraries have their own layout that is independent of the main binary. Layout optimization is most effective for statically linked code.
- **Build system integration**: Using `-ffunction-sections` and a section ordering file requires changes to the build system. BOLT is simpler to integrate as a post-link step.
- **Marginal benefit for small binaries**: If the entire `.text` section fits in L1 I-cache (32KB), function reordering provides negligible benefit.
- **Profile representativeness**: The ordering is only as good as the profiling workload. Different workloads may have different hot function sets and call patterns.
- **Interaction with LTO**: Link-Time Optimization can change function boundaries through inlining and outlining. Apply function reordering after LTO for best results.
