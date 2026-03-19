#include "common.h"
#include <cstdint>
#include <cstring>
#include <vector>

namespace profiler {

// ============================================================
// Memory bandwidth — sequential read of a large array
// ============================================================

static double measure_bandwidth() {
    // 64 MB — should be larger than any LLC
    constexpr size_t SIZE = 64ULL * 1024 * 1024;
    constexpr size_t COUNT = SIZE / sizeof(uint64_t);
    constexpr int TRIALS = 5;

    std::vector<uint64_t> buf(COUNT);
    // Initialize to avoid page faults during measurement
    memset(buf.data(), 0xAB, SIZE);

    std::vector<double> bw_samples;
    bw_samples.reserve(TRIALS);

    for (int t = 0; t < TRIALS; ++t) {
        uint64_t sum = 0;
        const uint64_t* p = buf.data();

        clobber();
        uint64_t c0 = rdcycle();

        // Sequential read — compiler should not auto-vectorize due to volatile-like escape
        for (size_t i = 0; i < COUNT; i += 8) {
            sum += p[i + 0];
            sum += p[i + 1];
            sum += p[i + 2];
            sum += p[i + 3];
            sum += p[i + 4];
            sum += p[i + 5];
            sum += p[i + 6];
            sum += p[i + 7];
        }

        uint64_t c1 = rdcycle();
        escape(sum);

        double cycles = (double)(c1 - c0);
        double bytes_per_cycle = (double)SIZE / cycles;
        bw_samples.push_back(bytes_per_cycle);
    }

    auto stats = compute_stats(bw_samples);
    return stats.median;
}

// ============================================================
// TLB miss penalty — compare within-page vs cross-page stride
// ============================================================

static double measure_tlb_miss_penalty() {
    // Use a large array so we span many pages
    constexpr size_t SIZE = 64ULL * 1024 * 1024;
    constexpr int ACCESSES = 1 << 18; // 256K accesses
    constexpr int TRIALS = 5;

    std::vector<uint8_t> buf(SIZE, 0);
    volatile uint8_t* p = buf.data();

    // --- Stride 64: stays within pages, few TLB misses ---
    std::vector<double> intra_samples;
    intra_samples.reserve(TRIALS);
    for (int t = 0; t < TRIALS; ++t) {
        size_t pos = 0;
        clobber();
        uint64_t c0 = rdcycle();
        for (int i = 0; i < ACCESSES; ++i) {
            (void)p[pos];
            pos += 64;
            if (pos >= SIZE) pos = 0;
        }
        uint64_t c1 = rdcycle();
        intra_samples.push_back((double)(c1 - c0) / ACCESSES);
    }

    // --- Stride 4096: one access per page, maximizes TLB misses ---
    std::vector<double> inter_samples;
    inter_samples.reserve(TRIALS);
    for (int t = 0; t < TRIALS; ++t) {
        size_t pos = 0;
        clobber();
        uint64_t c0 = rdcycle();
        for (int i = 0; i < ACCESSES; ++i) {
            (void)p[pos];
            pos += 4096;
            if (pos >= SIZE) pos = 0;
        }
        uint64_t c1 = rdcycle();
        inter_samples.push_back((double)(c1 - c0) / ACCESSES);
    }

    auto intra_stats = compute_stats(intra_samples);
    auto inter_stats = compute_stats(inter_samples);

    // TLB miss penalty is the difference: per-page access latency minus
    // within-page access latency. The sequential stride-64 pattern keeps
    // TLB entries warm, while stride-4096 causes a miss on every access
    // once we exceed TLB reach.
    double penalty = inter_stats.median - intra_stats.median;
    if (penalty < 0) penalty = 0; // sanity

    return penalty;
}

// ============================================================
// Entry point
// ============================================================

void measure_memory() {
    double bw = measure_bandwidth();
    record("memory_system", "bandwidth_bytes_per_cycle", bw);

    double tlb_penalty = measure_tlb_miss_penalty();
    record("memory_system", "tlb_miss_penalty", tlb_penalty);
}

} // namespace profiler
