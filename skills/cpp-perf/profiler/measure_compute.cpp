#include "common.h"
#include <cstdint>

namespace profiler {

// On macOS/Apple Silicon, large inline asm blocks cause "fixup value out of range"
// because the assembler generates branches that exceed ARM's ±128MB offset.
// Solution: use C loops with escape() to prevent optimization, which are
// more portable and still accurate enough for latency/throughput measurement.

// ============================================================
// Instruction Latency — dependent chains force serial execution
// ============================================================

static double measure_int_add_latency() {
    constexpr int CHAIN = 10000;
    return measure_cycles_per_op([&]() {
        uint64_t val = 1;
        for (int i = 0; i < CHAIN; i++) {
            val = val + 1;  // carried dependency
        }
        escape(val);
    }, CHAIN, 200, 20);
}

static double measure_int_mul_latency() {
    constexpr int CHAIN = 10000;
    return measure_cycles_per_op([&]() {
        uint64_t val = 7;
        for (int i = 0; i < CHAIN; i++) {
            val = val * 3;  // carried dependency
        }
        escape(val);
    }, CHAIN, 200, 20);
}

static double measure_int_div_latency() {
    constexpr int CHAIN = 5000;
    return measure_cycles_per_op([&]() {
        uint64_t val = 0x7FFFFFFFFFFFFFFFULL;
        for (int i = 0; i < CHAIN; i++) {
            val = val / 7;
            if (val == 0) val = 0x7FFFFFFFFFFFFFFFULL;
        }
        escape(val);
    }, CHAIN, 100, 10);
}

static double measure_fp_add_latency() {
    constexpr int CHAIN = 10000;
    return measure_cycles_per_op([&]() {
        double val = 1.0;
        for (int i = 0; i < CHAIN; i++) {
            val = val + 0.5;
        }
        escape(val);
    }, CHAIN, 200, 20);
}

static double measure_fp_mul_latency() {
    constexpr int CHAIN = 10000;
    return measure_cycles_per_op([&]() {
        double val = 1.0000001;
        for (int i = 0; i < CHAIN; i++) {
            val = val * 1.0000001;
        }
        escape(val);
    }, CHAIN, 200, 20);
}

static double measure_fp_div_latency() {
    constexpr int CHAIN = 5000;
    return measure_cycles_per_op([&]() {
        double val = 1e18;
        for (int i = 0; i < CHAIN; i++) {
            val = val / 1.0000001;
        }
        escape(val);
    }, CHAIN, 100, 10);
}

// ============================================================
// Instruction Throughput — independent operations saturate units
// ============================================================

static double measure_int_add_throughput() {
    constexpr int OPS = 8 * 1000;
    return measure_cycles_per_op([&]() {
        uint64_t a=1, b=2, c=3, d=4, e=5, f=6, g=7, h=8;
        for (int i = 0; i < 1000; i++) {
            a += 1; b += 1; c += 1; d += 1;
            e += 1; f += 1; g += 1; h += 1;
        }
        escape(a); escape(b); escape(c); escape(d);
        escape(e); escape(f); escape(g); escape(h);
    }, OPS, 200, 20);
}

static double measure_int_mul_throughput() {
    constexpr int OPS = 8 * 1000;
    return measure_cycles_per_op([&]() {
        uint64_t a=1, b=2, c=3, d=4, e=5, f=6, g=7, h=8;
        for (int i = 0; i < 1000; i++) {
            a *= 3; b *= 3; c *= 3; d *= 3;
            e *= 3; f *= 3; g *= 3; h *= 3;
        }
        escape(a); escape(b); escape(c); escape(d);
        escape(e); escape(f); escape(g); escape(h);
    }, OPS, 200, 20);
}

static double measure_fp_add_throughput() {
    constexpr int OPS = 8 * 1000;
    return measure_cycles_per_op([&]() {
        double a=1, b=2, c=3, d=4, e=5, f=6, g=7, h=8;
        for (int i = 0; i < 1000; i++) {
            a += 0.5; b += 0.5; c += 0.5; d += 0.5;
            e += 0.5; f += 0.5; g += 0.5; h += 0.5;
        }
        escape(a); escape(b); escape(c); escape(d);
        escape(e); escape(f); escape(g); escape(h);
    }, OPS, 200, 20);
}

static double measure_fp_mul_throughput() {
    constexpr int OPS = 8 * 1000;
    return measure_cycles_per_op([&]() {
        double a=1.0001, b=1.0001, c=1.0001, d=1.0001;
        double e=1.0001, f=1.0001, g=1.0001, h=1.0001;
        for (int i = 0; i < 1000; i++) {
            a *= 1.0000001; b *= 1.0000001; c *= 1.0000001; d *= 1.0000001;
            e *= 1.0000001; f *= 1.0000001; g *= 1.0000001; h *= 1.0000001;
        }
        escape(a); escape(b); escape(c); escape(d);
        escape(e); escape(f); escape(g); escape(h);
    }, OPS, 200, 20);
}

// ============================================================
// Entry point
// ============================================================

void measure_compute() {
    record("instructions.integer", "add_lat", measure_int_add_latency());
    record("instructions.integer", "mul_lat", measure_int_mul_latency());
    record("instructions.integer", "div_lat", measure_int_div_latency());
    record("instructions.fp",      "fadd_lat", measure_fp_add_latency());
    record("instructions.fp",      "fmul_lat", measure_fp_mul_latency());
    record("instructions.fp",      "fdiv_lat", measure_fp_div_latency());

    record("instructions.integer", "add_tp", measure_int_add_throughput());
    record("instructions.integer", "mul_tp", measure_int_mul_throughput());
    record("instructions.fp",      "fadd_tp", measure_fp_add_throughput());
    record("instructions.fp",      "fmul_tp", measure_fp_mul_throughput());
}

} // namespace profiler
