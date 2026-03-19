---
name: Measurement Methodology — How to Benchmark Without Lying to Yourself
source: perf-book Ch.2
layers: [system]
platforms: [arm, x86]
keywords: [measurement, benchmark, warmup, variance, stddev, frequency scaling, DoNotOptimize, methodology]
---

## Problem

The majority of performance "improvements" reported in the wild are measurement artifacts. Getting reliable numbers is harder than writing the optimization itself. The most common mistakes, each of which can produce 10-50% phantom speedups or slowdowns:

1. **No warmup:** the first iteration pays for page faults, I-cache/D-cache cold misses, DVFS ramp-up, and JIT compilation (in managed runtimes). Measuring it inflates the "before" number.
2. **Reporting mean instead of median:** a single outlier (context switch, timer interrupt, GC pause) pulls the mean by 5-20%. Median is robust.
3. **Dead code elimination:** the compiler removes the computation you're trying to benchmark because its result is unused. You measure an empty loop.
4. **Turbo Boost / DVFS skewing results:** the CPU runs at different frequencies between runs. Wall-clock comparisons become meaningless.
5. **Insufficient iterations:** running 3 iterations and reporting the best one is not a benchmark, it's a random number generator.

## Detection

**Source-level indicators:**
- Benchmark loop that does not use `benchmark::DoNotOptimize()` or `asm volatile("" ::: "memory")` on the result
- No warmup phase before the measured region
- Using `clock()` or `gettimeofday()` instead of `std::chrono::steady_clock` or `clock_gettime(CLOCK_MONOTONIC)`
- Reporting a single number (no variance information)
- Benchmark binary compiled without optimization (`-O0`)

**Profile-level indicators:**
- Run-to-run variance > 10% of the mean
- Suspiciously large speedup (> 2x) from a minor code change on a hot loop
- Results that don't reproduce on a different machine or after reboot

## Transformation

### Rule 1: Always warmup

Run at least 1 full iteration (ideally 3-5) before starting the clock. This fills caches, triggers DVFS ramp-up, and faults in pages.

```cpp
// Bad: cold start included in measurement
auto start = steady_clock::now();
for (int i = 0; i < N; i++) compute(data);
auto elapsed = steady_clock::now() - start;

// Good: warmup then measure
for (int i = 0; i < 3; i++) compute(data);  // warmup, result discarded
auto start = steady_clock::now();
for (int i = 0; i < N; i++) compute(data);
auto elapsed = steady_clock::now() - start;
```

### Rule 2: Report median + p99 + stddev, not just mean

Collect at least 10 samples. Report the median (50th percentile), p99, and relative standard deviation (stddev / mean). The median tells you typical performance, p99 tells you worst-case jitter.

```cpp
std::vector<double> samples;
for (int trial = 0; trial < 30; trial++) {
    auto t0 = steady_clock::now();
    compute(data);
    auto t1 = steady_clock::now();
    samples.push_back(duration_cast<nanoseconds>(t1 - t0).count());
}
std::sort(samples.begin(), samples.end());
double median = samples[samples.size() / 2];
double p99    = samples[(int)(samples.size() * 0.99)];
double mean   = accumulate(samples.begin(), samples.end(), 0.0) / samples.size();
double stddev = /* compute */;
double rel_stddev = stddev / mean;
```

### Rule 3: Prevent dead code elimination

The compiler WILL remove any computation whose result is not observable. Use barriers.

```cpp
// Google Benchmark style
#include <benchmark/benchmark.h>

static void BM_compute(benchmark::State& state) {
    auto data = prepare_data();
    for (auto _ : state) {
        auto result = compute(data);
        benchmark::DoNotOptimize(result);  // prevents DCE
        benchmark::ClobberMemory();        // forces memory writes to complete
    }
}

// Manual barrier (no library dependency)
float result = compute(data);
asm volatile("" : "+r"(result));       // scalar: mark result as used
asm volatile("" ::: "memory");         // force stores to be visible
```

### Rule 4: Disable frequency scaling for microbenchmarks

CPU frequency varies between runs (turbo boost, thermal throttling, power saving). For microbenchmarks, lock the frequency.

```bash
# Linux: set performance governor (requires root)
sudo cpupower frequency-set -g performance

# Verify:
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
# should print "performance"

# Alternative: disable turbo boost specifically
echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo     # Intel
echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost              # AMD

# Apple Silicon: fixed frequency, no action needed (most deterministic platform)
```

### Rule 5: Don't report speedup if stddev > 10% of mean

If relative standard deviation exceeds 10%, the noise floor is too high. Either increase iteration count, pin to a core (`taskset -c 0`), or reduce system load.

```bash
# Pin process to core 0 on Linux
taskset -c 0 ./benchmark

# Disable address space randomization (reduces variance)
echo 0 | sudo tee /proc/sys/kernel/randomize_va_space
```

### Rule 6: Run until relative stddev < 5% (adaptive iteration count)

Don't hardcode iteration count. Increase iterations until measurements converge.

```cpp
int iterations = 100;
double rel_stddev;
do {
    auto samples = run_benchmark(iterations);
    rel_stddev = compute_rel_stddev(samples);
    if (rel_stddev > 0.05) iterations *= 2;
} while (rel_stddev > 0.05 && iterations < 1000000);
```

### Rule 7: Compare instruction counts alongside wall time

Wall time is noisy. Instruction counts are deterministic (same input = same count, regardless of frequency or load). Use both.

```bash
# Collect instruction count and cycles
perf stat -e instructions,cycles,task-clock ./benchmark

# Compare:
# - If instructions changed: code generation changed (expected)
# - If instructions same but cycles changed: microarchitectural effect (cache, branch pred)
# - If both same but wall time changed: system noise (not a real difference)
```

## Expected Impact

- **Eliminating warmup bias:** 5-30% correction on small-dataset benchmarks where cold cache dominates
- **Proper statistics:** eliminates false positives from noisy data; prevents shipping "optimizations" that are within noise
- **DCE prevention:** prevents measuring nothing and reporting infinity-x speedup
- **Frequency locking:** reduces run-to-run variance from 15-30% to < 3%

## Caveats

- **Warmup length varies:** L3-resident data needs 1-2 warmup iterations. Page-table-heavy workloads may need 5+.
- **Performance governor affects power consumption:** don't leave it on permanently on laptops or production servers.
- **`DoNotOptimize` is not free:** it introduces a register move or memory store. For sub-nanosecond operations, this overhead becomes significant. Amortize by batching work.
- **Instruction count is not IPC:** two programs with the same instruction count can have wildly different runtimes if one has more cache misses or branch mispredictions. Always pair instruction count with cycles or wall time.
- **Microbenchmarks vs. real workloads:** a function that is fast in isolation may be slow in context due to I-cache/D-cache pressure from surrounding code. Microbenchmarks overestimate improvements.
