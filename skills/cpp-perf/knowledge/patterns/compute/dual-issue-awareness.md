---
name: Dual-Issue Awareness — Which Instructions Can Execute in Parallel
source: MegPeak aarch64_dual_issue.h
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [dual issue, instruction pairing, port, execution unit, ldr, fmla, co-issue, superscalar]
---

## Problem

Modern superscalar CPUs can issue multiple instructions per cycle — but only if those instructions use **different execution ports**. Writing code that accidentally puts two instructions on the same port every cycle halves your throughput, even though the code looks parallel.

This is especially critical on ARM in-order cores (Cortex-A55) and narrow OoO cores (Cortex-A76/A78 with 4-wide dispatch), where the scheduling is less forgiving than on wide Intel cores.

The key insight from MegPeak's measurement methodology: **you cannot look up dual-issue behavior in a manual. You must measure it.** ARM's Software Optimization Guides list per-instruction throughput, but they do not tell you which instruction *pairs* can co-issue. MegPeak measures this systematically.

## Detection

**Source-level indicators:**
- Inner loop that interleaves loads and computes but achieves lower throughput than expected
- Vectorized loop where loads and FP operations should overlap but don't
- Hand-written NEON/SVE code with seemingly optimal instruction count but poor throughput

**Profile-level indicators:**
- IPC of 1.0-1.5 on a 2-wide or 4-wide core (should be higher)
- Throughput matches the slower instruction's throughput (not overlapped)
- `llvm-mca` shows instructions piling up on the same port

## Transformation

### Methodology: How to Discover Dual-Issue Pairs

From MegPeak's approach (`aarch64_dual_issue.h`):

**Step 1:** Measure individual instruction throughput in isolation.

```cpp
// Measure FMLA throughput alone (should be ~1/cycle on A55, ~2/cycle on A78)
// Repeat FMLA with independent operands, measure cycles / instruction
THROUGHPUT_BENCH(fmla_only, 100,
    "fmla v0.4s, v1.4s, v2.4s \n"
    "fmla v3.4s, v4.4s, v5.4s \n"
    // ... repeat with independent registers
)

// Measure LDR d throughput alone
THROUGHPUT_BENCH(ldr_d_only, 100,
    "ldr d0, [x0] \n"
    "ldr d1, [x0, #8] \n"
    // ...
)
```

**Step 2:** Measure the pair together.

```cpp
// Measure LDR d + FMLA interleaved
THROUGHPUT_BENCH(ldr_d_fmla, 100,
    "ldr d6, [x0] \n"
    "fmla v0.4s, v1.4s, v2.4s \n"
    "ldr d7, [x0, #8] \n"
    "fmla v3.4s, v4.4s, v5.4s \n"
    // ...
)
```

**Step 3:** Compare. If `pair_throughput == max(individual_A, individual_B)`, they dual-issue. If `pair_throughput == individual_A + individual_B`, they do NOT dual-issue (serialized).

### Known Dual-Issue Results (Cortex-A55, from MegPeak)

| Pair | Dual-Issue? | Evidence |
|------|------------|----------|
| `ldr d` + `fmla` | YES | ldr_d=1c, fmla=1c, pair=1c |
| `ldr q` + `fmla` | NO | ldr_q=2c (128-bit load takes 2 cycles on A55's 64-bit bus), pair=2c = ldr_q alone |
| `ins` + `fmla v.4s[lane]` | YES | ins=1c, fmla_lane=1c, pair=1c |
| `ldr d` + `ldr d` | YES | 2 loads/cycle possible |
| `fmla` + `fmla` | NO (A55) | Single FP pipe, 1 fmla/cycle |
| `ldr q` + `ldr q` | NO (A55) | 128-bit load occupies both load slots for 2 cycles |

### Known Dual-Issue Results (Cortex-A78 / Neoverse V1)

| Pair | Dual-Issue? | Evidence |
|------|------------|----------|
| `ldr q` + `fmla` | YES | Wide load bus, ldr_q=1c, fmla=1c, pair=1c |
| `fmla` + `fmla` | YES | 2 FP pipes, 2 fmla/cycle |
| `ldr q` + `ldr q` | YES | 2 load pipes |
| `ldr q` + `str q` | YES | Separate load/store pipes |

### Practical Implication: A55 Kernel Design

On Cortex-A55, the `ldr q` + `fmla` non-dual-issue is a critical design constraint. The standard GEMM microkernel pattern of:

```asm
ldr q0, [x0], #16     ; load A vector (128-bit)
ldr q1, [x1], #16     ; load B vector (128-bit)
fmla v2.4s, v0.4s, v1.4s
```

is suboptimal because `ldr q` blocks `fmla`. The A55-optimized pattern uses 64-bit loads:

```asm
; A55-optimized: use ldr d (64-bit) which can dual-issue with fmla
ldr d0, [x0], #8      ; load lower half of A (dual-issues with fmla below)
fmla v2.4s, v4.4s, v0.s[0]   ; uses previous data
ldr d1, [x0], #8      ; load upper half of A
fmla v3.4s, v4.4s, v0.s[1]
; ... process 64-bit chunks to maintain dual-issue
```

This restructuring can yield 40-80% speedup on A55 with zero algorithmic change.

### x86 Perspective: Port Assignment

On x86, the dual-issue question is about port assignment. Intel CPUs have 6-8 execution ports:

```
Port 0: ALU, FMA, FP mul
Port 1: ALU, FMA, FP add
Port 2: Load (+ AGU)
Port 3: Load (+ AGU)
Port 4: Store data
Port 5: ALU, shuffle, branch
Port 6: ALU, branch
Port 7: Store AGU (Skylake+)
```

Two FMAs can dual-issue (ports 0+1). Two loads can dual-issue (ports 2+3). An FMA + load can dual-issue (port 0 + port 2). But two shuffles cannot (both need port 5).

Check port assignment using `llvm-mca --resource-pressure` or the uops.info tables.

### Writing Dual-Issue Friendly Code

```cpp
// Bad: all loads first, then all computes (serial phases)
for (int i = 0; i < N; i += 16) {
    auto a0 = vld1q_f32(A + i);
    auto a1 = vld1q_f32(A + i + 4);
    auto a2 = vld1q_f32(A + i + 8);
    auto a3 = vld1q_f32(A + i + 12);
    // All loads done. Now all FMLAs compete for the same port.
    c0 = vfmaq_f32(c0, a0, b0);
    c1 = vfmaq_f32(c1, a1, b1);
    c2 = vfmaq_f32(c2, a2, b2);
    c3 = vfmaq_f32(c3, a3, b3);
}

// Good: interleave loads and computes (dual-issue friendly)
for (int i = 0; i < N; i += 16) {
    auto a0 = vld1q_f32(A + i);
    c0 = vfmaq_f32(c0, a0, b0);   // load and fmla on different ports
    auto a1 = vld1q_f32(A + i + 4);
    c1 = vfmaq_f32(c1, a1, b1);
    auto a2 = vld1q_f32(A + i + 8);
    c2 = vfmaq_f32(c2, a2, b2);
    auto a3 = vld1q_f32(A + i + 12);
    c3 = vfmaq_f32(c3, a3, b3);
}
```

Note: on wide OoO cores (A78, Skylake), the hardware reorders instructions to fill ports optimally. Interleaving mostly matters for in-order cores (A55) and narrow OoO cores.

## Expected Impact

- **A55 ldr d vs ldr q:** 40-80% speedup on compute kernels by switching from 128-bit to 64-bit loads to enable dual-issue with FMLA
- **Load/compute interleaving on in-order cores:** 20-50% speedup by enabling instruction pairing
- **x86 port conflict resolution:** 10-30% speedup by choosing instructions that map to different ports (e.g., using `lea` instead of `add`+`shl` to free port 0 for FMA)

## Caveats

- **OoO cores reschedule automatically:** on Cortex-A78/X1 and Intel Skylake+, the hardware reorder buffer handles instruction scheduling. Manual interleaving has minimal impact on these cores. Focus on in-order (A55) and narrow OoO (A76) cores.
- **Dual-issue tables are not in official documentation:** ARM's Software Optimization Guide lists per-instruction throughput but does not document instruction pairing rules. The only reliable method is measurement (MegPeak's approach).
- **Dual-issue behavior varies by core revision:** A55r1 may differ from A55r0. Always measure on the exact target hardware.
- **Register pressure from interleaving:** interleaving loads and computes increases the number of live registers (the loaded value must survive until the compute uses it). On register-starved code, this can cause spills that negate the dual-issue benefit.
- **Compiler-generated code:** compilers may or may not interleave loads and computes. For in-order cores, inspect the generated assembly and consider using intrinsics or inline assembly for critical inner loops.
