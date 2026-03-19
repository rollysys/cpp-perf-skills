#pragma once
#include <chrono>
#include <vector>
#include <string>
#include <map>
#include <algorithm>
#include <cmath>
#include <functional>
#include <cstdio>
#include <csignal>
#include <csetjmp>

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
// Run a function N times, collect timing, return median in nanoseconds
template <typename Fn>
double measure_ns(Fn fn, int iterations = 1000, int warmup = 100) {
    for (int i = 0; i < warmup; i++) fn();

    std::vector<double> samples;
    samples.reserve(iterations);
    double freq = (double)cycle_freq();

    for (int i = 0; i < iterations; i++) {
        uint64_t c0 = rdcycle();
        fn();
        uint64_t c1 = rdcycle();
        // Convert ticks to nanoseconds: ticks * 1e9 / freq
        samples.push_back((double)(c1 - c0) * 1e9 / freq);
    }

    auto stats = compute_stats(samples);
    return stats.median;
}

// Measure nanoseconds per operation when fn performs `ops_per_call` operations
template <typename Fn>
double measure_ns_per_op(Fn fn, int ops_per_call, int iterations = 1000, int warmup = 100) {
    return measure_ns(fn, iterations, warmup) / ops_per_call;
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

// Legacy aliases
template <typename Fn>
double measure_cycles(Fn fn, int iterations = 1000, int warmup = 100) {
    return measure_ns(fn, iterations, warmup);
}
template <typename Fn>
double measure_cycles_per_op(Fn fn, int ops_per_call, int iterations = 1000, int warmup = 100) {
    return measure_ns_per_op(fn, ops_per_call, iterations, warmup);
}

// ============================================================
// Calibration: convert nanoseconds to CPU cycles
// ============================================================
// Detect CPU frequency from OS, then ns_to_cycles = ns * freq_ghz

} // temporarily close namespace for system headers
#if defined(__APPLE__)
#include <sys/sysctl.h>
#endif
namespace profiler { // reopen

inline double get_cpu_freq_ghz() {
    static double freq = []() -> double {
#if defined(__APPLE__)
        uint64_t freq_hz = 0;
        size_t sz = sizeof(freq_hz);
        if (sysctlbyname("hw.cpufrequency_max", &freq_hz, &sz, nullptr, 0) == 0 && freq_hz > 0)
            return freq_hz / 1e9;
#elif defined(__linux__)
        FILE* f = fopen("/proc/cpuinfo", "r");
        if (f) {
            char line[256];
            while (fgets(line, sizeof(line), f)) {
                double mhz;
                if (sscanf(line, "cpu MHz : %lf", &mhz) == 1) {
                    fclose(f);
                    return mhz / 1000.0;
                }
            }
            fclose(f);
        }
#endif
        // Fallback: calibrate using a tight dependent-add chain.
        // We know int add = 1 cycle on any modern CPU.
        // Measure how long 10M dependent adds take in ns → ns_per_add ≈ ns_per_cycle.
        constexpr int N = 10000000;
        uint64_t val = 1;
        auto t0 = Clock::now();
        for (int i = 0; i < N; i++) {
            val = val + 7;  // data dependency: each add depends on previous result
            asm volatile("" : "+r"(val));  // prevent optimization, keep dependency
        }
        auto t1 = Clock::now();
        escape(val);
        double elapsed_ns = (double)std::chrono::duration_cast<ns>(t1 - t0).count();
        // Each iteration = 1 cycle (add) + ~0 cycles (asm barrier is compiler-only)
        double ns_per_cycle = elapsed_ns / N;
        double ghz = 1.0 / ns_per_cycle;
        // Sanity clamp: 0.5 - 6.0 GHz
        if (ghz < 0.5) ghz = 0.5;
        if (ghz > 6.0) ghz = 6.0;
        return ghz;
    }();
    return freq;
}

inline double calibrate_ns_per_cycle() {
    return 1.0 / get_cpu_freq_ghz();
}

inline double ns_to_cycles(double ns) {
    return ns / calibrate_ns_per_cycle();
}

// ============================================================
// SIGILL-safe instruction probing
// Try to execute an instruction; if it causes SIGILL, return false.
// Used to detect platform support before running measurements.
// ============================================================
inline jmp_buf sigill_jmp;
inline volatile bool sigill_caught;

inline void sigill_handler(int) {
    sigill_caught = true;
    longjmp(sigill_jmp, 1);
}

// Try running fn(). Returns true if it ran without SIGILL, false if instruction not supported.
template <typename Fn>
inline bool try_instruction(Fn fn) {
    sigill_caught = false;
    struct sigaction sa_new{}, sa_old{};
    sa_new.sa_handler = sigill_handler;
    sigemptyset(&sa_new.sa_mask);
    sa_new.sa_flags = 0;
    sigaction(SIGILL, &sa_new, &sa_old);

    if (setjmp(sigill_jmp) == 0) {
        fn();
    }

    sigaction(SIGILL, &sa_old, nullptr);
    return !sigill_caught;
}

// Measure instruction, skip with warning if not supported on this platform
template <typename Fn>
inline void measure_or_skip(const std::string& section, const std::string& key,
                            Fn measure_fn, const char* name) {
    auto test_fn = [&]() { measure_fn(); };  // dry run to check support
    if (try_instruction(test_fn)) {
        record(section, key, measure_fn());
    } else {
        fprintf(stderr, "    [skip] %s not supported on this CPU\n", name);
    }
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
