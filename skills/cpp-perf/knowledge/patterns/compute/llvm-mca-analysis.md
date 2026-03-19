---
name: Static Performance Analysis with llvm-mca
source: perf-book Ch.5
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [llvm-mca, UICA, static analysis, throughput, bottleneck, port pressure, resource conflict]
---

## Problem

Profiling tells you *where* time is spent, but not *why*. When you have a hot inner loop, you need to know which microarchitectural resource is the bottleneck: is it a dependency chain? A port conflict? An insufficient number of execution units? Running the program with `perf` gives you symptoms, not root causes.

**llvm-mca** (LLVM Machine Code Analyzer) answers this question without running the program at all. Feed it the assembly of a hot loop, and it simulates the CPU's pipeline to report:
- Throughput bottleneck (cycles per iteration)
- Per-port pressure (which execution ports are saturated)
- Dependency chains (which instructions are on the critical path)
- Resource conflicts (when two instructions compete for the same port)

This is **deterministic** (same assembly = same report, every time), **fast** (milliseconds), and **requires no target hardware** (you can analyze ARM code on an x86 dev machine).

## Detection

**When to use llvm-mca:**
- You have a hot loop identified by profiling and want to know the bottleneck
- You are comparing two assembly sequences to predict which is faster
- You want to verify that your manual optimization (unrolling, instruction reordering) actually improved the pipeline utilization
- You are writing intrinsics or inline assembly and want to validate the schedule

**Limitations (important to know before using):**
- No cache miss modeling: llvm-mca assumes all loads hit L1. If your loop is memory-bound, llvm-mca's throughput estimate is meaningless.
- No branch prediction modeling: it assumes all branches are correctly predicted.
- Microarchitecture model accuracy varies: Intel models are good, ARM models are improving but less precise.

## Transformation

### Step 1: Extract the assembly of the hot loop

```bash
# From an object file or binary
objdump -d --no-show-raw-insn binary | \
  awk '/^[0-9a-f]+ <hot_function>:$/,/^$/' > func.s

# From Clang directly (compile to assembly)
clang -O2 -S -mcpu=cortex-a78 hot_module.cpp -o hot_module.s

# From Compiler Explorer (godbolt.org): copy the assembly of the hot loop
```

Trim the assembly to just the loop body. llvm-mca needs the instructions between the loop label and the branch back. Remove directives and non-instruction lines.

```asm
# Example: extracted loop body for llvm-mca (ARM AArch64)
# File: loop.s
ldr   q0, [x0], #16
ldr   q1, [x1], #16
fmla  v2.4s, v0.4s, v1.4s
subs  x2, x2, #4
b.ne  loop
```

### Step 2: Run llvm-mca

```bash
# Basic analysis targeting a specific CPU
llvm-mca --mcpu=cortex-a78 --iterations=100 < loop.s

# With timeline view (shows instruction scheduling per cycle)
llvm-mca --mcpu=cortex-a78 --timeline --iterations=10 < loop.s

# With resource pressure view (shows port utilization)
llvm-mca --mcpu=cortex-a78 --resource-pressure --iterations=100 < loop.s

# For x86 (Intel)
llvm-mca --mcpu=skylake --iterations=100 < loop_x86.s
llvm-mca --mcpu=icelake-server --iterations=100 < loop_x86.s

# All views at once
llvm-mca --mcpu=cortex-a78 \
  --timeline --resource-pressure --bottleneck-analysis \
  --iterations=100 < loop.s
```

### Step 3: Read the output

**Summary section:**
```
Iterations:        100
Instructions:      500
Total Cycles:      204
Total uOps:        500

Dispatch Width:    4
uOps Per Cycle:    2.45
IPC:               2.45
Block RThroughput: 2.0
```

Key numbers:
- **Block RThroughput:** theoretical best-case cycles per iteration (reciprocal throughput). This is the ceiling.
- **Total Cycles / Iterations:** actual simulated cycles per iteration. If this is higher than RThroughput, there is a pipeline stall.
- **IPC:** if IPC << dispatch width, the pipeline is underutilized.

**Resource pressure table:**
```
Resource pressure per iteration:
[0]    [1]    [2]    [3]    [4]    [5]
1.00   1.00   2.00   1.00   0.00   1.00

[0] - Port 0 (ALU)
[1] - Port 1 (ALU)
[2] - Port 2 (FP/SIMD)   <-- bottleneck: 2.0 uops, max throughput = 2/cycle
[3] - Port 3 (Load)
[5] - Port 5 (Store)
```

If one port has pressure >= RThroughput, that port is the throughput bottleneck.

**Timeline view (first few iterations):**
```
[0,0]     DeER .    .   ldr   q0, [x0], #16
[0,1]     DeER .    .   ldr   q1, [x1], #16
[0,2]     D=eeeER   .   fmla  v2.4s, v0.4s, v1.4s
[0,3]     DeE--R    .   subs  x2, x2, #4
[0,4]     D=eE-R    .   b.ne  loop
```

Legend: `D`=dispatch, `e`=execute, `E`=complete, `R`=retire, `=`=stall (waiting for operand), `-`=stall (waiting for retirement).

### Step 4: Act on the results

**Bottleneck: dependency chain** (Total Cycles >> RThroughput, low IPC)
→ Break the dependency. Use multiple accumulators. See `unroll-factor-formula.md`.

**Bottleneck: port pressure** (one port at 100% while others idle)
→ Use instructions that map to different ports. On x86: replace `imul` (port 1 only) with `lea` (ports 1, 5) if possible.

**Bottleneck: dispatch width** (IPC = dispatch width, all ports busy)
→ You are at the throughput ceiling. Reduce instruction count (strength reduction, FMA fusion).

### UICA: Alternative for Intel x86

For Intel microarchitectures, [UICA](https://uica.uops.info/) (uops.info Code Analyzer) has more accurate models than llvm-mca, especially for recent Intel cores:

```bash
# Online tool: https://uica.uops.info/
# Paste assembly, select microarchitecture, get analysis

# Or use the command-line version
python3 uica.py --arch SKL < loop_x86.s
```

UICA is based on measured port mappings from uops.info, which are more accurate than LLVM's scheduling models for Intel cores.

### Example: Identifying a dependency chain bottleneck

```bash
$ cat reduction.s
fmla v0.4s, v1.4s, v2.4s    # v0 depends on previous v0

$ llvm-mca --mcpu=neoverse-v1 --bottleneck-analysis < reduction.s
# ...
# Bottleneck: data dependency on operand v0
# Block RThroughput: 0.5 (from resource pressure)
# Actual throughput: 4.0 cycles (limited by FMLA latency = 4)
```

The report shows RThroughput = 0.5 cycles (2 FMLA/cycle possible) but actual = 4.0 cycles (latency-bound). This is an 8x gap — exactly what `unroll-factor-formula.md` predicts: need 8 independent accumulators.

## Expected Impact

- **Time to diagnose:** reduces "why is this loop slow?" from hours of guessing to minutes of reading a report
- **No hardware needed:** analyze ARM code on your x86 dev laptop, or analyze a CPU you don't have yet
- **Deterministic:** same assembly always produces the same analysis, unlike profiling which is noisy
- **Guides optimization:** tells you exactly whether to unroll, reorder, or change instruction selection

## Caveats

- **L1-hit assumption:** llvm-mca assumes perfect cache. For memory-bound loops, the analysis is overly optimistic. Use it only for compute-bound bottleneck identification.
- **No branch prediction:** loop-carried branches are assumed always correctly predicted. For loops with data-dependent branches, llvm-mca underestimates the cost.
- **ARM model accuracy:** LLVM's scheduling models for ARM cores (especially Cortex-A55, A76) are less mature than for Intel cores. Cross-validate with actual measurements for ARM targets.
- **Marker comments required for complex loops:** llvm-mca processes instructions linearly. For complex control flow (if/else in the loop), you may need to extract only the hot path.
- **Cannot model memory disambiguation or store forwarding:** these microarchitectural effects can significantly impact real performance but are not modeled.
- **Loop body only:** llvm-mca analyzes a basic block in steady state. It does not model loop setup/teardown overhead, which matters for short-trip-count loops.
