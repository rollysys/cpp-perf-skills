#include "common.h"
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <numeric>
#include <random>

namespace profiler {

// ============================================================
// Pointer-chase latency for cache hierarchy detection
// ============================================================

// Build a random cyclic permutation of indices within the array.
// Each element array[i] holds the index of the next element to visit.
// This ensures every element is visited exactly once per cycle,
// defeating hardware prefetchers.
static void build_pointer_chase(uint64_t* arr, size_t count) {
    // Create a sequential list, then Fisher-Yates shuffle to form a single cycle.
    std::vector<size_t> order(count);
    std::iota(order.begin(), order.end(), 0);

    std::mt19937_64 rng(0xdeadbeef);
    // Sattolo's algorithm: guaranteed single cycle
    for (size_t i = count - 1; i > 0; --i) {
        std::uniform_int_distribution<size_t> dist(0, i - 1);
        size_t j = dist(rng);
        std::swap(order[i], order[j]);
    }

    // Convert permutation to pointer-chase: arr[order[k]] = order[k+1]
    for (size_t k = 0; k < count - 1; ++k) {
        arr[order[k]] = order[k + 1];
    }
    arr[order[count - 1]] = order[0]; // close the cycle
}

// Chase pointers for `accesses` steps, return average cycles per access.
static double chase_latency(uint64_t* arr, size_t count, int accesses) {
    // Warm up: touch all elements once
    uint64_t idx = 0;
    for (size_t i = 0; i < count; ++i) {
        idx = arr[idx];
    }

    // Timed run
    clobber();
    uint64_t c0 = rdcycle();
    idx = 0;
    for (int i = 0; i < accesses; ++i) {
        idx = arr[idx];
    }
    uint64_t c1 = rdcycle();
    escape(idx);

    return (double)(c1 - c0) / accesses;
}

// ============================================================
// Cache line size detection via stride experiments
// ============================================================

// Access a small array (fits in L1) with varying strides.
// When stride >= cache line size, every access is to a new line,
// so latency stays flat. When stride < line size, multiple accesses
// hit the same line, yielding lower average latency.
// The transition point reveals the cache line size.
static int detect_cache_line_size() {
    constexpr size_t SIZE = 16 * 1024; // 16 KB — fits comfortably in L1
    constexpr int ACCESSES = 1 << 20;

    std::vector<uint8_t> buf(SIZE, 0);
    volatile uint8_t* p = buf.data();

    double prev_lat = 0.0;
    int line_size = 64; // default fallback

    int strides[] = {4, 8, 16, 32, 64, 128, 256};

    for (int stride : strides) {
        size_t mask = SIZE - 1; // SIZE must be power of 2
        size_t pos = 0;

        clobber();
        uint64_t c0 = rdcycle();
        for (int i = 0; i < ACCESSES; ++i) {
            (void)p[pos];
            pos = (pos + stride) & mask;
        }
        uint64_t c1 = rdcycle();
        double lat = (double)(c1 - c0) / ACCESSES;

        // The first stride where latency jumps significantly (>1.3x)
        // compared to previous stride indicates we crossed into a new
        // cache line on every access.
        if (prev_lat > 0 && lat > prev_lat * 1.3 && stride <= 128) {
            line_size = stride;
        }
        prev_lat = lat;
    }

    return line_size;
}

// ============================================================
// Main cache measurement
// ============================================================

void measure_cache() {
    // Sizes to test (in bytes)
    struct SizeEntry {
        size_t bytes;
        const char* label;
    };
    SizeEntry sizes[] = {
        {  2 * 1024, "2KB"},
        {  4 * 1024, "4KB"},
        {  8 * 1024, "8KB"},
        { 16 * 1024, "16KB"},
        { 32 * 1024, "32KB"},
        { 64 * 1024, "64KB"},
        {128 * 1024, "128KB"},
        {256 * 1024, "256KB"},
        {512 * 1024, "512KB"},
        {  1 * 1024 * 1024, "1MB"},
        {  2 * 1024 * 1024, "2MB"},
        {  4 * 1024 * 1024, "4MB"},
        {  8 * 1024 * 1024, "8MB"},
        { 16 * 1024 * 1024, "16MB"},
        { 32 * 1024 * 1024, "32MB"},
    };
    constexpr int NUM_SIZES = sizeof(sizes) / sizeof(sizes[0]);
    constexpr int ACCESSES = 1 << 20; // ~1M pointer chases per size

    double latencies[NUM_SIZES];

    for (int i = 0; i < NUM_SIZES; ++i) {
        size_t count = sizes[i].bytes / sizeof(uint64_t);
        std::vector<uint64_t> arr(count);
        build_pointer_chase(arr.data(), count);

        // Multiple trials, take median
        std::vector<double> trials;
        trials.reserve(5);
        for (int t = 0; t < 5; ++t) {
            trials.push_back(chase_latency(arr.data(), count, ACCESSES));
        }
        auto stats = compute_stats(trials);
        latencies[i] = stats.median;

        // Record raw data point
        char key[64];
        snprintf(key, sizeof(key), "lat_%s", sizes[i].label);
        record("cache.latency_curve", key, latencies[i]);
    }

    // --- Detect cache boundaries ---
    // Look for jumps >1.5x in latency curve
    double l1_lat = latencies[0];
    double l2_lat = 0, l3_lat = 0;
    int l1_size_kb = 0, l2_size_kb = 0, l3_size_kb = 0;
    bool found_l2 = false, found_l3 = false;

    for (int i = 1; i < NUM_SIZES; ++i) {
        double ratio = latencies[i] / latencies[i - 1];
        if (!found_l2 && ratio > 1.5) {
            // Previous size was the last to fit in L1
            l1_size_kb = sizes[i - 1].bytes / 1024;
            l1_lat = latencies[i - 1];
            l2_lat = latencies[i];
            found_l2 = true;
        } else if (found_l2 && !found_l3 && ratio > 1.5) {
            // Previous size was the last to fit in L2
            l2_size_kb = sizes[i - 1].bytes / 1024;
            l3_lat = latencies[i];
            found_l3 = true;
        }
    }

    // If L3 boundary found, estimate L3 size as the last size before latency
    // fully plateaus (or just use the largest tested size as lower bound)
    if (found_l3) {
        // Find where latency stops increasing significantly
        for (int i = NUM_SIZES - 1; i > 0; --i) {
            double ratio = latencies[i] / latencies[i - 1];
            if (ratio > 1.3) {
                l3_size_kb = sizes[i - 1].bytes / 1024;
                break;
            }
        }
        if (l3_size_kb == 0) {
            l3_size_kb = sizes[NUM_SIZES - 1].bytes / 1024;
        }
    }

    // Fallback: if boundaries not clearly detected, use common defaults
    if (l1_size_kb == 0) {
        // Didn't find clear L1 boundary — probably 32KB or 64KB
        // Use the point where latency first starts rising
        l1_size_kb = 32;
        l1_lat = latencies[3]; // 16KB should be in L1
    }
    if (!found_l2) {
        // Estimate from common knowledge
        l2_size_kb = 256;
        l2_lat = latencies[7]; // 256KB
    }

    // --- Cache line size ---
    int line_size = detect_cache_line_size();

    // --- Record results ---
    record("cache.l1d", "size_kb", l1_size_kb);
    record("cache.l1d", "latency", l1_lat);
    record("cache.l1d", "line_bytes", line_size);

    if (found_l2 || l2_size_kb > 0) {
        record("cache.l2", "size_kb", l2_size_kb);
        record("cache.l2", "latency", l2_lat);
    }

    if (found_l3) {
        record("cache.l3", "size_kb", l3_size_kb);
        record("cache.l3", "latency", l3_lat);
    }
}

} // namespace profiler
