#include "common.h"
#include <cstdint>
#include <random>
#include <vector>

namespace profiler {

// ============================================================
// Branch misprediction penalty
//
// Compare a perfectly predictable branch pattern (all true)
// against a random 50/50 pattern. The branch predictor can
// perfectly predict the former but only gets ~50% right on
// the latter.
//
// penalty = (random_time - predictable_time) / (N * 0.5)
//
// The 0.5 factor accounts for the ~50% misprediction rate
// on truly random data.
// ============================================================

void measure_branch() {
    constexpr int N = 1 << 20; // 1M branches
    constexpr int TRIALS = 7;

    // --- Build predictable array (all 1s) ---
    std::vector<uint8_t> predictable(N, 1);

    // --- Build random 50/50 array ---
    std::vector<uint8_t> random_arr(N);
    std::mt19937_64 rng(12345);
    std::uniform_int_distribution<int> coin(0, 1);
    for (int i = 0; i < N; ++i) {
        random_arr[i] = coin(rng);
    }

    // --- Measure predictable branches ---
    auto run_branches = [&](const uint8_t* arr) -> double {
        std::vector<double> samples;
        samples.reserve(TRIALS);

        // Warmup to train the branch predictor
        for (int w = 0; w < 3; ++w) {
            uint64_t s = 0;
            for (int i = 0; i < N; ++i) {
                if (arr[i]) s++;
            }
            escape(s);
        }

        for (int t = 0; t < TRIALS; ++t) {
            uint64_t s = 0;
            clobber();
            uint64_t c0 = rdcycle();
            for (int i = 0; i < N; ++i) {
                if (arr[i]) {
                    s++;
                }
            }
            uint64_t c1 = rdcycle();
            escape(s);
            samples.push_back((double)(c1 - c0));
        }

        auto stats = compute_stats(samples);
        return stats.median;
    };

    double pred_cycles = run_branches(predictable.data());
    double rand_cycles = run_branches(random_arr.data());

    // The additional cost comes entirely from mispredictions.
    // With random data, ~50% of branches are mispredicted.
    double extra_cycles = rand_cycles - pred_cycles;
    if (extra_cycles < 0) extra_cycles = 0; // sanity guard

    double mispredictions = N * 0.5;
    double penalty = extra_cycles / mispredictions;

    record("branch", "mispredict_penalty", penalty);

    // Also record raw per-branch costs for reference
    record("branch", "predictable_cycles_per_branch", pred_cycles / N);
    record("branch", "random_cycles_per_branch", rand_cycles / N);
}

} // namespace profiler
