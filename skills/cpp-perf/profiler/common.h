#pragma once
#include <chrono>
#include <vector>
#include <string>
#include <map>
#include <algorithm>
#include <cmath>
#include <functional>
#include <cstdio>

#if defined(__APPLE__) && defined(__aarch64__)
#include <mach/mach_time.h>
#endif

namespace profiler {

// ============================================================
// Timing
// ============================================================
using Clock = std::chrono::steady_clock;
using ns = std::chrono::nanoseconds;

inline long long now_ns() {
    return std::chrono::duration_cast<ns>(Clock::now().time_since_epoch()).count();
}

// Cycle counter (platform-specific)
#if defined(__APPLE__) && defined(__aarch64__)
inline uint64_t rdcycle() {
    return mach_absolute_time();
}
inline uint64_t cycle_freq() {
    mach_timebase_info_data_t info;
    mach_timebase_info(&info);
    // mach_absolute_time returns ticks; ticks * numer / denom = nanoseconds
    // We want ticks/second: 1e9 * denom / numer
    return (uint64_t)(1e9 * info.denom / info.numer);
}
#elif defined(__aarch64__)
// Linux ARM: cntvct_el0 generic timer
inline uint64_t rdcycle() {
    uint64_t val;
    asm volatile("mrs %0, cntvct_el0" : "=r"(val));
    return val;
}
inline uint64_t cycle_freq() {
    uint64_t freq;
    asm volatile("mrs %0, cntfrq_el0" : "=r"(freq));
    return freq;
}
#elif defined(__x86_64__)
inline uint64_t rdcycle() {
    uint32_t lo, hi;
    asm volatile("rdtsc" : "=a"(lo), "=d"(hi));
    return ((uint64_t)hi << 32) | lo;
}
inline uint64_t cycle_freq() {
    // TSC frequency needs calibration; approximate with steady_clock
    auto t0 = Clock::now();
    uint64_t c0 = rdcycle();
    // Spin for ~10ms
    while (std::chrono::duration_cast<ns>(Clock::now() - t0).count() < 10000000) {}
    uint64_t c1 = rdcycle();
    auto t1 = Clock::now();
    double elapsed_s = std::chrono::duration_cast<ns>(t1 - t0).count() / 1e9;
    return (uint64_t)((c1 - c0) / elapsed_s);
}
#else
inline uint64_t rdcycle() { return 0; }
inline uint64_t cycle_freq() { return 1; }
#endif

// ============================================================
// Statistics
// ============================================================
struct Stats {
    double min, median, mean, p99, stddev;
};

inline Stats compute_stats(std::vector<double>& samples) {
    std::sort(samples.begin(), samples.end());
    Stats s;
    s.min = samples.front();
    s.median = samples[samples.size() / 2];
    s.mean = 0;
    for (auto v : samples) s.mean += v;
    s.mean /= samples.size();
    s.p99 = samples[(size_t)(samples.size() * 0.99)];
    double var = 0;
    for (auto v : samples) var += (v - s.mean) * (v - s.mean);
    s.stddev = std::sqrt(var / samples.size());
    return s;
}

// ============================================================
// Results collection
// ============================================================
// Hierarchical map: section -> key -> value (in cycles)
using ResultMap = std::map<std::string, std::map<std::string, double>>;

// Global results
inline ResultMap& results() {
    static ResultMap r;
    return r;
}

inline void record(const std::string& section, const std::string& key, double cycles) {
    results()[section][key] = cycles;
}

// ============================================================
// Measurement helpers
// ============================================================
// Run a function N times, collect timing, return median cycles
template <typename Fn>
double measure_cycles(Fn fn, int iterations = 1000, int warmup = 100) {
    for (int i = 0; i < warmup; i++) fn();

    std::vector<double> samples;
    samples.reserve(iterations);
    double freq = (double)cycle_freq();

    for (int i = 0; i < iterations; i++) {
        uint64_t c0 = rdcycle();
        fn();
        uint64_t c1 = rdcycle();
        samples.push_back((double)(c1 - c0));
    }

    auto stats = compute_stats(samples);
    return stats.median;
}

// Measure cycles per operation when fn performs `ops_per_call` operations
template <typename Fn>
double measure_cycles_per_op(Fn fn, int ops_per_call, int iterations = 1000, int warmup = 100) {
    return measure_cycles(fn, iterations, warmup) / ops_per_call;
}

// Prevent compiler from optimizing away a value
template <typename T>
inline void escape(T const& val) {
    asm volatile("" : : "r,m"(val) : "memory");
}

// Prevent reordering across this point
inline void clobber() {
    asm volatile("" ::: "memory");
}

// ============================================================
// Measurement module interface
// ============================================================
void measure_compute();
void measure_cache();
void measure_memory();
void measure_branch();
void measure_os();
void measure_alloc();
void measure_io();
void measure_ipc();
void output_yaml();

} // namespace profiler
