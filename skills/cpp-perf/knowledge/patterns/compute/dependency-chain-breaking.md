---
name: Breaking Data Dependency Chains
source: perf-ninja core_bound/dep_chains_1, dep_chains_2, perf-book Ch.9
layers: [microarchitecture]
platforms: [arm, x86]
keywords: [dependency chain, ILP, instruction level parallelism, accumulator, unroll, reduction, latency bound]
---

## Problem

Serial data dependency chains force the CPU to execute instructions sequentially, preventing out-of-order execution from exploiting instruction-level parallelism (ILP). This is most commonly seen in:

1. **Reduction loops** with a single accumulator: each `+=` depends on the previous one, forming a chain whose length equals the loop trip count multiplied by the operation latency.
2. **Linked-list pointer chasing**: accessing node N+1 requires dereferencing node N first. Even with perfect cache locality (e.g., arena allocation), the dependency chain serializes execution.
3. **Recurrent state in sequential generators**: e.g., an XorShift RNG where each output depends on the previous internal state via a chain of shift+XOR operations.

When the critical path latency exceeds the throughput capacity of the execution units, the loop is **latency-bound** rather than throughput-bound, and functional units sit idle.

## Detection

**Source-level indicators:**
- A single accumulator variable updated every iteration: `sum += a[i]`
- Linked-list traversal: `node = node->next`
- Sequential state machines / RNGs where output N depends on output N-1
- Any loop-carried variable that feeds back into itself each iteration

**Profiling indicators:**
- Low IPC despite high `Retiring` percentage (on x86 TMA: `Core Bound > Ports Utilization`)
- Large gap between theoretical throughput and observed throughput
- On ARM (Apple M1 example from perf-book): IPC of 4.0 when hardware can sustain 7+ for the instruction mix

**Assembly-level indicators (ARM example from dep_chains_2):**
```asm
; XorShift32::gen() -- 3 dependent eor+shift pairs = 6-cycle chain
eor    w0, w0, w0, lsl #13
eor    w0, w0, w0, lsr #17
eor    w0, w0, w0, lsl #5
; All subsequent fmul/fmadd for particle coordinates depend on w0
; but are NOT on the critical path -- they can overlap across iterations
```

**Rule of thumb:** If an instruction is on the critical path, its **latency** determines performance. If it is off the critical path, its **throughput** determines performance.

## Transformation

### Pattern 1: Multiple accumulators for reductions

**Before** -- single accumulator, latency-bound (FP add latency ~3-5 cycles):
```cpp
float sum = 0.0f;
for (int i = 0; i < N; i++) {
    sum += a[i];  // each add waits for the previous add to complete
}
```

**After** -- four independent accumulators, throughput-bound:
```cpp
float sum0 = 0.0f, sum1 = 0.0f, sum2 = 0.0f, sum3 = 0.0f;
int i = 0;
for (; i + 3 < N; i += 4) {
    sum0 += a[i];      // 4 independent dependency chains
    sum1 += a[i + 1];  // CPU can issue all 4 adds in parallel
    sum2 += a[i + 2];
    sum3 += a[i + 3];
}
for (; i < N; i++) {   // remainder
    sum0 += a[i];
}
float sum = sum0 + sum1 + sum2 + sum3;
```

The number of accumulators should match or exceed `latency / throughput` for the operation. For FP add with latency=4, throughput=0.5: need >= 8 accumulators to fully saturate. In practice 2-4 is often sufficient because the OOO window can overlap other work.

### Pattern 2: Overlapping linked-list traversals (dep_chains_1)

**Before** -- serial pointer chasing across two lists:
```cpp
// O(N^2): for each node in l1, scan all of l2
unsigned solution(List *l1, List *l2) {
    unsigned retVal = 0;
    List *head2 = l2;
    while (l1) {
        unsigned v = l1->value;
        l2 = head2;
        while (l2) {
            if (l2->value == v) {
                retVal += getSumOfDigits(v);
                break;
            }
            l2 = l2->next;  // dependency chain #1
        }
        l1 = l1->next;      // dependency chain #2 (but serialized)
    }
    return retVal;
}
```

**After** -- interleave multiple independent traversals:
```cpp
unsigned solution(List *l1, List *l2) {
    unsigned retVal = 0;
    List *head2 = l2;
    // Process two l1-nodes simultaneously to overlap pointer chasing
    while (l1 && l1->next) {
        unsigned v1 = l1->value;
        unsigned v2 = l1->next->value;
        List *p1 = head2;
        List *p2 = head2;
        // Interleaved inner traversals -- two independent chains
        while (p1 || p2) {
            if (p1) {
                if (p1->value == v1) { retVal += getSumOfDigits(v1); p1 = nullptr; }
                else p1 = p1->next;
            }
            if (p2) {
                if (p2->value == v2) { retVal += getSumOfDigits(v2); p2 = nullptr; }
                else p2 = p2->next;
            }
        }
        l1 = l1->next->next;
    }
    // handle remainder node
    // ...
    return retVal;
}
```

### Pattern 3: Breaking RNG dependency chains (dep_chains_2)

**Before** -- single RNG object creates a 6-cycle recurrent dependency per iteration:
```cpp
void particleMotion(vector<Particle> &particles, uint32_t seed) {
    XorShift32 rng(seed);
    for (int i = 0; i < STEPS; i++)
        for (auto &p : particles) {
            uint32_t angle = rng.gen();   // 6-cycle chain per call
            float angle_rad = angle * DEGREE_TO_RADIAN;
            p.x += cosine(angle_rad) * p.velocity;
            p.y += sine(angle_rad) * p.velocity;
        }
}
```

**After** -- two independent RNG objects, loop unrolled by 2:
```cpp
void particleMotion(vector<Particle> &particles,
                    uint32_t seed1, uint32_t seed2) {
    XorShift32 rng1(seed1);
    XorShift32 rng2(seed2);
    for (int i = 0; i < STEPS; i++) {
        for (int j = 0; j + 1 < particles.size(); j += 2) {
            uint32_t angle1 = rng1.gen();  // chain A
            float angle_rad1 = angle1 * DEGREE_TO_RADIAN;
            particles[j].x += cosine(angle_rad1) * particles[j].velocity;
            particles[j].y += sine(angle_rad1)   * particles[j].velocity;

            uint32_t angle2 = rng2.gen();  // chain B (independent)
            float angle_rad2 = angle2 * DEGREE_TO_RADIAN;
            particles[j+1].x += cosine(angle_rad2) * particles[j+1].velocity;
            particles[j+1].y += sine(angle_rad2)   * particles[j+1].velocity;
        }
        // handle remainder
    }
}
```

Key insight: with 2 chains on Apple M1, IPC jumps from 4.0 to 7.1, runtime drops from 19ms to 10ms -- nearly 2x speedup.

## Expected Impact

- **Reduction loops with multiple accumulators:** 2-4x speedup depending on operation latency vs throughput ratio. FP reductions on modern OOO CPUs typically see 2-3x.
- **Overlapping pointer chasing:** 1.5-2x speedup when memory latency allows overlap. Gains are bounded by memory bandwidth if lists are not cache-resident.
- **Breaking RNG / sequential state chains:** ~2x speedup per doubling of independent chains, up to hardware limits. On Apple M1, 2 chains saturate the execution units. Wider CPUs (e.g., Intel Golden Cove) may benefit from 3-4 chains.

The theoretical speedup is `min(num_chains, latency / throughput)`.

## Caveats

- **Floating-point associativity:** Multiple accumulators change the order of FP operations, producing slightly different results due to rounding. Requires `-ffast-math` or explicit acceptance of non-determinism. Use `#pragma clang fp reassociate(on)` to limit scope.
- **Register pressure:** Each additional chain requires its own set of live registers. Exceeding the architectural register count causes spills to stack, negating the benefit. ARM has 32 FP/SIMD registers; x86 has 16 (32 with AVX-512). Monitor for spills.
- **OOO window size:** If the dependency chain body is very long (thousands of instructions), the CPU's Reservation Station cannot "see" both chains simultaneously. In that case, you must **interleave** the chains at the statement level, not just place them sequentially. Compilers do not do this automatically.
- **Correctness for RNGs:** Splitting an RNG into multiple independent streams changes the generated sequence. This is acceptable for simulations with inherent randomness but not for deterministic replay scenarios.
- **Diminishing returns:** On Apple M1, going from 2 to 4 chains gave negligible improvement because throughput was already saturated. Always measure for the target platform.
- **Compiler autovectorization interaction:** Breaking dependency chains may enable the compiler to autovectorize (as observed in dep_chains_2 -- Clang started using SIMD once two chains were present). This is often a bonus, but verify the generated code.
