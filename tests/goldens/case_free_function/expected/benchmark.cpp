// cpp-perf auto-generated benchmark
// Target: sum_array — golden_case_free_function
#include <chrono>
#include <vector>
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <cmath>
#include <functional>
#include <utility>
#include <random>

// ============================================================
// Prevent compiler from optimizing away the result
// ============================================================
template <typename T>
__attribute__((noinline)) void do_not_optimize(T const& value) {
    asm volatile("" : : "r,m"(value) : "memory");
}

// Prevent reordering (use with void-returning functions)
inline void clobber() {
    asm volatile("" ::: "memory");
}

// ============================================================
// Test data setup
// ============================================================
std::vector<double> setup_data() {
    std::mt19937_64 rng(42);
    std::uniform_real_distribution<double> dist(-1000.0, 1000.0);
    std::vector<double> data(10000);
    for (auto& v : data) v = dist(rng);
    return data;
}

// ============================================================
// Input variants (optional — for data-sensitive benchmarks)
// When {{INPUT_VARIANTS}} is defined, benchmark runs once per variant
// and reports results separately.
// ============================================================
// (none — single-input mode)

// ============================================================
// Implementation under test
// ============================================================
double sum_array(const std::vector<double>& data) {
    double sum = 0.0;
    for (size_t i = 0; i < data.size(); i++) {
        sum += data[i];
    }
    return sum;
}

// ============================================================
// JSON output
// ============================================================
static void print_json(const char* func_name, const char* variant,
                       int iterations, int warmup,
                       const std::vector<long long>& timings) {
    std::vector<long long> sorted = timings;
    std::sort(sorted.begin(), sorted.end());

    long long min_val = sorted.front();
    long long median = sorted[sorted.size() / 2];
    double mean = 0;
    for (auto t : sorted) mean += t;
    mean /= sorted.size();
    long long p99 = sorted[(size_t)(sorted.size() * 0.99)];

    double variance = 0;
    for (auto t : sorted) variance += (t - mean) * (t - mean);
    double stddev = std::sqrt(variance / sorted.size());

    double cv = stddev / mean * 100.0;

    printf("{\n");
    printf("  \"function\": \"%s\",\n", func_name);
    if (variant && variant[0] != '\0') {
        printf("  \"variant\": \"%s\",\n", variant);
    }
    printf("  \"iterations\": %d,\n", iterations);
    printf("  \"warmup\": %d,\n", warmup);
    printf("  \"timings_ns\": [");
    for (size_t i = 0; i < timings.size(); i++) {
        if (i > 0) printf(",");
        printf("%lld", timings[i]);
    }
    printf("],\n");
    printf("  \"stats\": {\n");
    printf("    \"min\": %lld,\n", min_val);
    printf("    \"median\": %lld,\n", median);
    printf("    \"mean\": %.1f,\n", mean);
    printf("    \"p99\": %lld,\n", p99);
    printf("    \"stddev\": %.1f,\n", stddev);
    printf("    \"cv_pct\": %.1f,\n", cv);
    printf("    \"stable\": %s\n", cv < 5.0 ? "true" : "false");
    printf("  }\n");
    printf("}\n");
}

// ============================================================
// Main benchmark
// ============================================================
int main() {
    const int WARMUP = 100;
    const int ITERATIONS = 1000;

    auto data = setup_data();

    // Warmup
    for (int i = 0; i < WARMUP; i++) {
        auto result = sum_array(data);
        do_not_optimize(result);
    }

    // Warmup verification
    {
        auto tw0 = std::chrono::steady_clock::now();
        auto r = sum_array(data);
        do_not_optimize(r);
        auto tw1 = std::chrono::steady_clock::now();
        long long warmup_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(tw1 - tw0).count();
        (void)warmup_ns;
    }

    // Measure
    std::vector<long long> timings;
    timings.reserve(ITERATIONS);
    for (int i = 0; i < ITERATIONS; i++) {
        // no reset needed — sum_array does not mutate input
        auto t0 = std::chrono::steady_clock::now();
        auto result = sum_array(data);
        do_not_optimize(result);
        auto t1 = std::chrono::steady_clock::now();
        timings.push_back(
            std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count());
    }

    print_json("sum_array", "", ITERATIONS, WARMUP, timings);
    return 0;
}
