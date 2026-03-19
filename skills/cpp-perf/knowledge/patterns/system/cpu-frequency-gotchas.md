---
name: CPU Frequency Gotchas — When Your Clock Lies to You
source: perf-book Ch.2, Ch.12, MegPeak observations
layers: [system]
platforms: [arm, x86]
keywords: [frequency, turbo boost, DVFS, big.LITTLE, AVX-512 throttle, performance governor, cycles]
---

## Problem

CPU frequency is not constant. It changes based on thermal state, power budget, instruction mix, and number of active cores. Every wall-clock measurement is implicitly a frequency measurement too. If you don't control for frequency, your benchmarks are measuring the CPU's power management policy, not your code's performance.

Key gotchas that bite even experienced engineers:

### 1. Turbo Boost Inflates Single-Thread, Deflates Multi-Thread

Intel Turbo Boost (and AMD Precision Boost) raises frequency when few cores are active and thermal headroom exists. A single-threaded benchmark might run at 4.8 GHz while a multi-threaded benchmark on the same chip runs at 3.6 GHz — a 33% difference that has nothing to do with your code.

Consequence: single-threaded speedup numbers are not comparable with multi-threaded numbers unless frequency is locked.

### 2. ARM big.LITTLE: P-cores and E-cores Are Different CPUs

On ARM SoCs (Snapdragon, Exynos, Apple Silicon, MediaTek), there are two (or three) cluster types with different:
- **Frequencies:** P-core at 3.0 GHz vs E-core at 2.0 GHz
- **Microarchitectures:** P-core (Cortex-A78) has 4-wide OoO vs E-core (Cortex-A55) has 2-wide in-order
- **IPC:** completely different. A cycle on A78 is not equivalent to a cycle on A55.

If the OS scheduler migrates your benchmark thread between clusters mid-run, you get nonsensical numbers.

### 3. DVFS Ramp-Up Delay

When a core wakes from idle or low-power state, DVFS (Dynamic Voltage and Frequency Scaling) takes 0.5-2 ms to ramp up to the target frequency. The first ~1 ms of computation runs at a lower frequency.

Consequence: short benchmarks (< 10 ms) are systematically biased by ramp-up. The "before" optimization code (which runs first) pays the ramp-up penalty; the "after" code runs at full frequency. This creates phantom speedups.

### 4. AVX-512 Frequency Downclocking (x86 pre-Ice Lake)

On Skylake-SP, Cascade Lake, and similar: executing heavy AVX-512 instructions triggers a frequency reduction of 100-300 MHz. This is the "P1/P2 license" system:
- **P0 (base):** no AVX-512
- **P1 (AVX-512 light):** -100 MHz
- **P2 (AVX-512 heavy, e.g., FMA):** -200 to -300 MHz

The frequency drop affects ALL code on that core, not just the AVX-512 instructions. If you interleave AVX-512 with scalar code, the scalar code runs slower too.

(See `avx512-throttling.md` for detailed treatment.)

### 5. Apple Silicon: The Exception

Apple M-series chips run P-cores and E-cores at fixed frequencies (no turbo boost). The frequency does not change based on workload. This makes Apple Silicon the most deterministic benchmarking platform available. However, big.LITTLE migration between P-cores and E-cores still applies.

## Detection

**Source-level indicators:**
- Benchmark does not set CPU governor to `performance`
- Benchmark does not pin to a specific core
- Short benchmark duration (< 100 ms total)
- Wall-clock comparisons without cycle-count verification

**Profile-level indicators:**
- `perf stat` shows different `GHz` between runs of the same benchmark
- Run-to-run variance > 5% despite pinning to a core
- Single-threaded speedup does not hold when running multi-threaded
- First benchmark iteration is consistently slower than subsequent iterations (DVFS ramp-up, separate from cache warmup)

**Diagnostic commands:**
```bash
# Check current frequency (Linux)
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq
watch -n 0.1 "cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq"

# Check governor
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor

# Check turbo boost status (Intel)
cat /sys/devices/system/cpu/intel_pstate/no_turbo

# Monitor frequency during benchmark (Linux)
perf stat -e cycles,task-clock ./benchmark
# Effective frequency = cycles / task-clock
```

## Transformation

### Fix 1: Lock frequency with performance governor

```bash
# Set all cores to maximum fixed frequency
sudo cpupower frequency-set -g performance

# Or set a specific frequency (e.g., base clock, no turbo)
sudo cpupower frequency-set -f 2400MHz

# Disable turbo boost (Intel)
echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo

# Disable boost (AMD)
echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost

# Verify
cpupower frequency-info
```

### Fix 2: Pin to a specific core

```bash
# Linux: pin to core 0 (which is a P-core on most configs)
taskset -c 0 ./benchmark

# Or use cset for isolation
sudo cset shield -c 0-3 -k on
sudo cset shield -e ./benchmark
```

### Fix 3: Measure in cycles, not nanoseconds

Hardware cycle counters (`rdtsc` on x86, `cntvct_el0` on ARM) count at a fixed rate regardless of DVFS on most modern CPUs (invariant TSC on x86, fixed-rate system counter on ARM).

```cpp
// x86: read invariant TSC
static inline uint64_t rdtsc() {
    uint32_t lo, hi;
    asm volatile("rdtsc" : "=a"(lo), "=d"(hi));
    return ((uint64_t)hi << 32) | lo;
}

// ARM: read system counter (CNTVCT_EL0)
static inline uint64_t read_cntvct() {
    uint64_t val;
    asm volatile("mrs %0, cntvct_el0" : "=r"(val));
    return val;
}

// Usage
uint64_t t0 = rdtsc();
compute(data);
uint64_t t1 = rdtsc();
uint64_t elapsed_cycles = t1 - t0;  // frequency-invariant
```

**Important:** `rdtsc` on modern Intel (Nehalem+) is an invariant TSC that ticks at a fixed rate (nominally the base frequency). It does NOT measure actual core cycles. To get actual core cycles, use `perf stat -e cycles` or `rdpmc`.

### Fix 4: DVFS warmup

```cpp
// Spin for 2ms before measuring to let DVFS ramp up
auto warmup_start = steady_clock::now();
volatile int sink = 0;
while (steady_clock::now() - warmup_start < 2ms) {
    for (int i = 0; i < 1000; i++) sink += i;
}
// Now the core is at full frequency — start measuring
```

### Fix 5: Verify frequency stability during measurement

```bash
# Run benchmark under perf stat and check effective frequency
perf stat -e task-clock,cycles,instructions ./benchmark

# Effective GHz = cycles / (task-clock * 1e6)
# If this varies between runs by > 1%, frequency is not stable
```

## Expected Impact

- **Locking frequency:** reduces run-to-run variance from 10-30% to 1-3%
- **Core pinning:** eliminates big.LITTLE migration artifacts and NUMA effects
- **DVFS warmup:** eliminates systematic bias in short benchmarks (especially A/B comparisons)
- **Cycle counting:** provides frequency-invariant measurements that are comparable across runs with different thermal states

## Caveats

- **Performance governor increases power consumption and heat:** do not leave it enabled on laptops (drains battery) or production servers (increases cooling costs). Set it only for the duration of benchmarking.
- **Disabling turbo boost reduces absolute performance:** your benchmark numbers with turbo disabled are lower than what users will see in production. Report both turbo-on (realistic) and turbo-off (reproducible) numbers.
- **Core pinning can hide NUMA effects:** if your production code accesses memory from multiple NUMA nodes, pinning to one core hides this cost. Pin to a core that represents the production memory topology.
- **Invariant TSC is not core cycles:** on x86, `rdtsc` ticks at a fixed rate regardless of core frequency. It measures wall time in units of the nominal frequency, not actual CPU cycles consumed. For actual cycle counting, use `perf stat -e cycles` or `rdpmc` with a fixed-function counter.
- **ARM system counter frequency varies by SoC:** `cntvct_el0` typically ticks at a lower frequency than the CPU clock (often ~19.2 MHz or ~24 MHz). It is a wall-clock timer, not a cycle counter. For cycle-accurate measurement on ARM, use PMU counters (`pmccntr_el0`) if accessible.
- **Container environments (Docker, K8s):** frequency governors may not be changeable from within a container. The host must configure them. `perf` may also be restricted.
