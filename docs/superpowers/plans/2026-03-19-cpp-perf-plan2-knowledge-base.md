# cpp-perf Knowledge Base — Implementation Plan (Plan 2 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract 14 structured optimization patterns from reference materials (books, labs, production code) into the `knowledge/patterns/` directory, providing the cpp-perf skill with source-backed optimization knowledge beyond LLM built-in capabilities.

**Architecture:** Each pattern is a standalone markdown file with YAML frontmatter (name, source, layers, platforms, keywords) and fixed sections (Problem, Detection, Transformation, Expected Impact, Caveats). Patterns are organized by category: vectorization, memory, branching, compute, system.

**Tech Stack:** Markdown with YAML frontmatter

**Spec:** `docs/superpowers/specs/2026-03-19-cpp-perf-skill-design.md` (Knowledge Sources section)

---

## File Structure

```
skills/cpp-perf/knowledge/patterns/
  vectorization/
    auto-vectorization-blockers.md
    manual-neon-idioms.md
    sve-scalable-patterns.md
  memory/
    aos-to-soa.md
    loop-tiling.md
    prefetch-strategies.md
    false-sharing.md
  branching/
    branch-to-cmov.md
    lookup-table-replace.md
  compute/
    dependency-chain-breaking.md
    fma-utilization.md
    strength-reduction.md
  system/
    huge-pages.md
    alignment.md
```

## Pattern File Template

Every pattern MUST follow this exact structure:

```markdown
---
name: <Pattern Name>
source: <reference source(s)>
layers: [<valid layers>]
platforms: [arm, x86]
keywords: [<search keywords for pre-filtering>]
---

## Problem
<What code pattern triggers this optimization — be specific about what to look for>

## Detection
<How to identify this in source code or disassembly — concrete patterns, not vague descriptions>

## Transformation
<Before/after code with explanation. MUST include actual C++ code examples>

## Expected Impact
<Quantified estimate referencing platform profile parameters like cache sizes, instruction latencies>

## Caveats
<When NOT to apply, edge cases, risks>
```

Valid `layers` values: `algorithm`, `language`, `microarchitecture`, `system`

---

### Task 1: Vectorization Patterns (3 files)

**Files:**
- Create: `skills/cpp-perf/knowledge/patterns/vectorization/auto-vectorization-blockers.md`
- Create: `skills/cpp-perf/knowledge/patterns/vectorization/manual-neon-idioms.md`
- Create: `skills/cpp-perf/knowledge/patterns/vectorization/sve-scalable-patterns.md`

**Sources to read:**

For auto-vectorization-blockers:
- `reference/perf-book/chapters/9-Optimizing-Computations/9-4 Vectorization.md`
- `reference/perf-book/chapters/9-Optimizing-Computations/9-3 Loop Optimizations.md`
- `reference/perf-ninja/labs/core_bound/vectorization_1/` (init.cpp, solution.cpp)
- `reference/perf-ninja/labs/core_bound/vectorization_2/` (init.cpp, solution.cpp)

For manual-neon-idioms:
- `reference/ComputeLibrary/src/cpu/kernels/pool3d/neon/fp32.cpp` (representative NEON kernel)
- `reference/ComputeLibrary/src/core/NEON/kernels/NEReductionOperationKernel.cpp`
- `reference/perf-ninja/labs/core_bound/compiler_intrinsics_1/` (init.cpp, solution.cpp)
- `reference/perf-book/chapters/9-Optimizing-Computations/9-5 Compiler Intrinsics.md`

For sve-scalable-patterns:
- `reference/optimized-routines/math/aarch64/sve/` (pick 2-3 files: log.c, exp.c, sin.c)
- `reference/optimized-routines/string/aarch64/memcpy-sve.S`
- Compare with AdvSIMD variants: `reference/optimized-routines/math/aarch64/advsimd/` (same functions)

- [ ] **Step 1: Read perf-book Ch.9 vectorization sections**

Read `reference/perf-book/chapters/9-Optimizing-Computations/9-4 Vectorization.md` and `9-3 Loop Optimizations.md`. Extract:
- Common auto-vectorization blockers (pointer aliasing, non-unit stride, data dependencies, function calls in loops, non-trivial control flow)
- How compiler reports vectorization failures
- Typical speedup ranges for vectorized vs scalar code

- [ ] **Step 2: Read perf-ninja vectorization labs**

Read `init.cpp` and `solution.cpp` from `reference/perf-ninja/labs/core_bound/vectorization_1/` and `vectorization_2/`. Extract the before/after patterns.

- [ ] **Step 3: Write auto-vectorization-blockers.md**

```markdown
---
name: Auto-Vectorization Blockers
source: perf-book Ch.9, perf-ninja core_bound/vectorization_1, vectorization_2
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [loop, vectorization, SIMD, scalar, auto-vectorize, restrict, alias, stride]
---
```

Content should cover: pointer aliasing (use `__restrict__`), non-unit stride access, loop-carried dependencies, function calls breaking vectorization, complex control flow, how to use `-fopt-info-vec-missed` / `-Rpass-missed=loop-vectorize` to diagnose. Include before/after code from perf-ninja labs.

- [ ] **Step 4: Read ComputeLibrary NEON kernels and perf-ninja intrinsics lab**

Read 2-3 ComputeLibrary NEON kernels and `reference/perf-ninja/labs/core_bound/compiler_intrinsics_1/` to extract common NEON intrinsic patterns.

- [ ] **Step 5: Write manual-neon-idioms.md**

```markdown
---
name: Manual NEON Intrinsic Idioms
source: ComputeLibrary NEON kernels, perf-ninja core_bound/compiler_intrinsics_1, perf-book Ch.9
layers: [microarchitecture]
platforms: [arm]
keywords: [NEON, intrinsics, vld1, vst1, vmul, vadd, float32x4_t, uint8x16_t, arm_neon.h]
---
```

Content should cover: common NEON data types, load/store patterns (vld1q/vst1q), arithmetic (vmulq/vaddq/vfmaq), horizontal operations, tail handling for non-multiple-of-4 sizes. Include real examples from ComputeLibrary.

- [ ] **Step 6: Read optimized-routines SVE implementations**

Read 2-3 SVE files from `reference/optimized-routines/math/aarch64/sve/` and compare with their AdvSIMD counterparts in `advsimd/`.

- [ ] **Step 7: Write sve-scalable-patterns.md**

```markdown
---
name: SVE Scalable Vector Patterns
source: optimized-routines math/aarch64/sve/, string/aarch64/memcpy-sve.S
layers: [microarchitecture]
platforms: [arm]
keywords: [SVE, scalable, predicate, whilelt, svfloat32_t, vector length agnostic, sve2]
---
```

Content should cover: SVE's VLA (vector-length agnostic) programming model, predicated operations, whilelt loop pattern, how SVE differs from fixed-width NEON, when to use SVE vs NEON.

- [ ] **Step 8: Commit**

```bash
git add skills/cpp-perf/knowledge/patterns/vectorization/
git commit -m "feat: add vectorization optimization patterns (3 files)"
```

---

### Task 2: Memory Patterns (4 files)

**Files:**
- Create: `skills/cpp-perf/knowledge/patterns/memory/aos-to-soa.md`
- Create: `skills/cpp-perf/knowledge/patterns/memory/loop-tiling.md`
- Create: `skills/cpp-perf/knowledge/patterns/memory/prefetch-strategies.md`
- Create: `skills/cpp-perf/knowledge/patterns/memory/false-sharing.md`

**Sources to read:**

For aos-to-soa:
- `reference/perf-ninja/labs/memory_bound/data_packing/` (init.cpp, solution.cpp)
- `reference/perf-book/chapters/8-Optimizing-Memory-Accesses/8-2 Cache-Friendly Data Structures.md`

For loop-tiling:
- `reference/perf-ninja/labs/memory_bound/loop_tiling_1/` (init.cpp, solution.cpp)
- `reference/perf-book/chapters/8-Optimizing-Memory-Accesses/8-1 Optimizing Memory Accesses.md`

For prefetch-strategies:
- `reference/perf-ninja/labs/memory_bound/swmem_prefetch_1/` (init.cpp, solution.cpp)
- `reference/perf-book/chapters/8-Optimizing-Memory-Accesses/8-6 Memory Prefetching.md`

For false-sharing:
- `reference/perf-ninja/labs/memory_bound/false_sharing_1/` (init.cpp, solution.cpp)
- `reference/perf-book/chapters/8-Optimizing-Memory-Accesses/8-2 Cache-Friendly Data Structures.md`

- [ ] **Step 1: Read perf-ninja data_packing lab + perf-book cache-friendly data structures**

- [ ] **Step 2: Write aos-to-soa.md**

```markdown
---
name: Array of Structures to Structure of Arrays
source: perf-ninja memory_bound/data_packing, perf-book Ch.8
layers: [microarchitecture, system]
platforms: [arm, x86]
keywords: [AoS, SoA, struct, cache line, spatial locality, data packing, hot fields, cold fields]
---
```

- [ ] **Step 3: Read perf-ninja loop_tiling_1 lab + perf-book memory optimization**

- [ ] **Step 4: Write loop-tiling.md**

```markdown
---
name: Loop Tiling for Cache Locality
source: perf-book Ch.8, perf-ninja memory_bound/loop_tiling_1
layers: [microarchitecture, system]
platforms: [arm, x86]
keywords: [nested loop, 2D array, matrix, stride, cache miss, working set, tile, block]
---
```

- [ ] **Step 5: Read perf-ninja swmem_prefetch_1 lab + perf-book prefetching**

- [ ] **Step 6: Write prefetch-strategies.md**

```markdown
---
name: Software Memory Prefetching
source: perf-book Ch.8, perf-ninja memory_bound/swmem_prefetch_1
layers: [microarchitecture, system]
platforms: [arm, x86]
keywords: [prefetch, __builtin_prefetch, cache miss, latency hiding, stride, linked list, pointer chasing]
---
```

- [ ] **Step 7: Read perf-ninja false_sharing_1 lab**

- [ ] **Step 8: Write false-sharing.md**

```markdown
---
name: False Sharing Elimination
source: perf-ninja memory_bound/false_sharing_1, perf-book Ch.8
layers: [system]
platforms: [arm, x86]
keywords: [false sharing, cache line, thread, alignas, padding, multithreaded, contention, atomic]
---
```

- [ ] **Step 9: Commit**

```bash
git add skills/cpp-perf/knowledge/patterns/memory/
git commit -m "feat: add memory optimization patterns (4 files)"
```

---

### Task 3: Branching Patterns (2 files)

**Files:**
- Create: `skills/cpp-perf/knowledge/patterns/branching/branch-to-cmov.md`
- Create: `skills/cpp-perf/knowledge/patterns/branching/lookup-table-replace.md`

**Sources to read:**

For branch-to-cmov:
- `reference/perf-ninja/labs/bad_speculation/branches_to_cmov_1/` (init.cpp, solution.cpp)
- `reference/perf-book/chapters/10-Optimizing-Branch-Prediction/10-3 Replace branches with predication.md`
- `reference/perf-book/chapters/10-Optimizing-Branch-Prediction/10-2 Replace branches with arithmetic.md`

For lookup-table-replace:
- `reference/perf-ninja/labs/bad_speculation/lookup_tables_1/` (init.cpp, solution.cpp)
- `reference/perf-book/chapters/10-Optimizing-Branch-Prediction/10-1 Replace branches with lookup copy.md`

- [ ] **Step 1: Read perf-ninja branches_to_cmov_1 lab + perf-book predication**

- [ ] **Step 2: Write branch-to-cmov.md**

```markdown
---
name: Replace Branches with Conditional Moves
source: perf-ninja bad_speculation/branches_to_cmov_1, perf-book Ch.10
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [branch, cmov, csel, predication, branch misprediction, conditional, ternary, branchless]
---
```

- [ ] **Step 3: Read perf-ninja lookup_tables_1 lab + perf-book lookup copy**

- [ ] **Step 4: Write lookup-table-replace.md**

```markdown
---
name: Replace Branches with Lookup Tables
source: perf-ninja bad_speculation/lookup_tables_1, perf-book Ch.10
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [lookup table, switch, branch, LUT, jump table, indirect, precompute, dispatch]
---
```

- [ ] **Step 5: Commit**

```bash
git add skills/cpp-perf/knowledge/patterns/branching/
git commit -m "feat: add branching optimization patterns (2 files)"
```

---

### Task 4: Compute Patterns (3 files)

**Files:**
- Create: `skills/cpp-perf/knowledge/patterns/compute/dependency-chain-breaking.md`
- Create: `skills/cpp-perf/knowledge/patterns/compute/fma-utilization.md`
- Create: `skills/cpp-perf/knowledge/patterns/compute/strength-reduction.md`

**Sources to read:**

For dependency-chain-breaking:
- `reference/perf-ninja/labs/core_bound/dep_chains_1/` (init.cpp, solution.cpp)
- `reference/perf-ninja/labs/core_bound/dep_chains_2/` (init.cpp, solution.cpp)
- `reference/perf-book/chapters/9-Optimizing-Computations/9-1 Data Dependencies.md`

For fma-utilization:
- `reference/optimized-routines/math/aarch64/advsimd/` (pick 2 files: exp.c or log.c showing FMA usage)
- `reference/perf-book/chapters/9-Optimizing-Computations/9-4 Vectorization.md` (FMA section if present)

For strength-reduction:
- `reference/Cpp-High-Performance/Chapter03/` (linear_search.cpp, binary_search.cpp)
- `reference/Cpp-High-Performance/Chapter04/` (cache_thrashing.cpp, sum_scores.cpp)

- [ ] **Step 1: Read perf-ninja dep_chains labs + perf-book data dependencies**

- [ ] **Step 2: Write dependency-chain-breaking.md**

```markdown
---
name: Breaking Data Dependency Chains
source: perf-ninja core_bound/dep_chains_1, dep_chains_2, perf-book Ch.9
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [dependency chain, ILP, instruction level parallelism, accumulator, unroll, reduction, latency bound]
---
```

- [ ] **Step 3: Read optimized-routines math functions for FMA patterns**

Read 2 files from `reference/optimized-routines/math/aarch64/advsimd/` to see how FMA instructions are used in polynomial evaluation and other math computations.

- [ ] **Step 4: Write fma-utilization.md**

```markdown
---
name: Fused Multiply-Add Utilization
source: optimized-routines math/aarch64/advsimd/, perf-book Ch.9
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [FMA, fused multiply-add, fmla, vfmadd, polynomial, Horner, multiply-accumulate, MAC]
---
```

- [ ] **Step 5: Read Cpp-High-Performance examples**

Read examples from Chapter03 and Chapter04 showing strength reduction patterns (replacing expensive operations with cheaper equivalents).

- [ ] **Step 6: Write strength-reduction.md**

```markdown
---
name: Strength Reduction
source: Cpp-High-Performance Ch.3-4
layers: [algorithm, language]
platforms: [arm, x86]
keywords: [strength reduction, division, multiplication, shift, modulo, power of two, lookup, precompute]
---
```

- [ ] **Step 7: Commit**

```bash
git add skills/cpp-perf/knowledge/patterns/compute/
git commit -m "feat: add compute optimization patterns (3 files)"
```

---

### Task 5: System Patterns (2 files)

**Files:**
- Create: `skills/cpp-perf/knowledge/patterns/system/huge-pages.md`
- Create: `skills/cpp-perf/knowledge/patterns/system/alignment.md`

**Sources to read:**

For huge-pages:
- `reference/perf-ninja/labs/memory_bound/huge_pages_1/` (init.cpp, solution.cpp)
- `reference/perf-book/chapters/8-Optimizing-Memory-Accesses/8-5 Reducing DTLB misses.md`

For alignment:
- `reference/perf-ninja/labs/memory_bound/mem_alignment_1/` (init.cpp, solution.cpp)
- `reference/perf-book/chapters/8-Optimizing-Memory-Accesses/8-1 Optimizing Memory Accesses.md`
- `reference/Cpp-High-Performance/Chapter07/alignment.cpp`

- [ ] **Step 1: Read perf-ninja huge_pages_1 lab + perf-book DTLB**

- [ ] **Step 2: Write huge-pages.md**

```markdown
---
name: Huge Pages for TLB Optimization
source: perf-ninja memory_bound/huge_pages_1, perf-book Ch.8
layers: [system]
platforms: [arm, x86]
keywords: [huge pages, TLB, DTLB, madvise, MADV_HUGEPAGE, mmap, MAP_HUGETLB, page fault, 2MB page]
---
```

- [ ] **Step 3: Read perf-ninja mem_alignment_1 lab + perf-book + Cpp-High-Performance alignment**

- [ ] **Step 4: Write alignment.md**

```markdown
---
name: Memory Alignment for Performance
source: perf-book Ch.8, perf-ninja memory_bound/mem_alignment_1, Cpp-High-Performance Ch.7
layers: [system, microarchitecture]
platforms: [arm, x86]
keywords: [alignment, alignas, aligned_alloc, cache line, 64-byte, split load, SIMD alignment, posix_memalign]
---
```

- [ ] **Step 5: Commit**

```bash
git add skills/cpp-perf/knowledge/patterns/system/
git commit -m "feat: add system optimization patterns (2 files)"
```

---

### Task 6: Final Validation

- [ ] **Step 1: Verify all 14 pattern files exist**

Run: `find skills/cpp-perf/knowledge/patterns -name '*.md' | sort`

Expected: 14 files across 5 directories.

- [ ] **Step 2: Verify all pattern files have valid frontmatter**

For each file, check:
- Has `---` delimiters
- Has all required fields: name, source, layers, platforms, keywords
- `layers` values are from valid set: algorithm, language, microarchitecture, system
- Has all required sections: Problem, Detection, Transformation, Expected Impact, Caveats

- [ ] **Step 3: Verify cpp-perf.md Stage 2 can find patterns**

The skill instructions in `skills/cpp-perf/cpp-perf.md` Stage 2 Step 2 use:
`skills/cpp-perf/knowledge/patterns/<layer>/*.md`

Verify the directory names match the valid layer values:
- `vectorization/` patterns have `layers: [microarchitecture]` → found under `patterns/microarchitecture/`? NO — they're under `patterns/vectorization/`.

**Important**: The Glob in Stage 2 uses `<layer>` from the analysis layers (algorithm, language, microarchitecture, system), but patterns are organized by topic (vectorization, memory, branching, compute, system). These don't directly map!

The skill should either:
a) Search ALL pattern directories regardless of layer, OR
b) Pattern directories should match layer names

Since patterns span multiple layers (e.g., loop-tiling is both microarchitecture and system), option (a) is simpler: just Glob `skills/cpp-perf/knowledge/patterns/**/*.md` and use `keywords` for matching.

**Action**: Note this mismatch. No code change needed for Plan 2 — this is a Plan 1 cpp-perf.md adjustment that should be made separately.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "feat: knowledge base complete — 14 optimization patterns from references"
```
