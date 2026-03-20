// cpp-perf auto-generated benchmark
// Target: scale_and_clamp — golden_case_vector_transform
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
std::vector<float> setup_data() {
    std::mt19937 rng(42);
    std::uniform_real_distribution<float> dist(-2.0f, 2.0f);
    std::vector<float> data(10000);
    for (auto& v : data) v = dist(rng);
    return data;
}

// ============================================================
// Input variants (optional — for data-sensitive benchmarks)
// ============================================================
// (none — single-input mode)

// ============================================================
// Implementation under test
// ============================================================
void scale_and_clamp(std::vector<float>& data, float scale, float min_val, float max_val) {
    for (size_t i = 0; i < data.size(); i++) {
        data[i] *= scale;
        if (data[i] < min_val) data[i] = min_val;
        if (data[i] > max_val) data[i] = max_val;
    }
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

    // Warmup — void-returning function, use clobber()
    for (int i = 0; i < WARMUP; i++) {
        auto tmp = setup_data();
        scale_and_clamp(tmp, 2.5f, -1.0f, 1.0f);
        clobber();
    }

    // Warmup verification
    {
        auto tmp = setup_data();
        auto tw0 = std::chrono::steady_clock::now();
        scale_and_clamp(tmp, 2.5f, -1.0f, 1.0f);
        clobber();
        auto tw1 = std::chrono::steady_clock::now();
        long long warmup_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(tw1 - tw0).count();
        (void)warmup_ns;
    }

    // Measure — must reset data each iteration (scale_and_clamp mutates input)
    std::vector<long long> timings;
    timings.reserve(ITERATIONS);
    for (int i = 0; i < ITERATIONS; i++) {
        data = setup_data();
        auto t0 = std::chrono::steady_clock::now();
        scale_and_clamp(data, 2.5f, -1.0f, 1.0f);
        clobber();
        auto t1 = std::chrono::steady_clock::now();
        timings.push_back(
            std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count());
    }

    print_json("scale_and_clamp", "", ITERATIONS, WARMUP, timings);
    return 0;
}
