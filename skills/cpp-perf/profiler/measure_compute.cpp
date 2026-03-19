#include "common.h"
#include <cstdint>

namespace profiler {

// Use volatile-style escape after every operation to prevent
// the compiler from strength-reducing or eliminating the dependency chain.
// The key insight: escape() after each iteration forces the compiler
// to actually compute each step, but adds minimal overhead since it's
// just an asm barrier (no memory access).

// ============================================================
// Instruction Latency — dependent chains force serial execution
// ============================================================

static double measure_int_add_latency() {
    constexpr int CHAIN = 100000;
    return measure_cycles_per_op([&]() {
        uint64_t val = 1;
        for (int i = 0; i < CHAIN; i++) {
            val = val + 1;
            clobber();  // prevent strength reduction
        }
        escape(val);
    }, CHAIN, 50, 5);
}

static double measure_int_mul_latency() {
    constexpr int CHAIN = 100000;
    return measure_cycles_per_op([&]() {
        uint64_t val = 7;
        for (int i = 0; i < CHAIN; i++) {
            val = val * 3;
            clobber();
        }
        escape(val);
    }, CHAIN, 50, 5);
}

static double measure_int_div_latency() {
    constexpr int CHAIN = 50000;
    return measure_cycles_per_op([&]() {
        uint64_t val = 0x7FFFFFFFFFFFFFFFULL;
        for (int i = 0; i < CHAIN; i++) {
            val = val / 7;
            if (val == 0) val = 0x7FFFFFFFFFFFFFFFULL;
            clobber();
        }
        escape(val);
    }, CHAIN, 20, 2);
}

static double measure_fp_add_latency() {
    constexpr int CHAIN = 100000;
    return measure_cycles_per_op([&]() {
        double val = 1.0;
        for (int i = 0; i < CHAIN; i++) {
            val = val + 0.5;
            clobber();
        }
        escape(val);
    }, CHAIN, 50, 5);
}

static double measure_fp_mul_latency() {
    constexpr int CHAIN = 100000;
    return measure_cycles_per_op([&]() {
        double val = 1.0000001;
        for (int i = 0; i < CHAIN; i++) {
            val = val * 1.0000001;
            clobber();
        }
        escape(val);
    }, CHAIN, 50, 5);
}

static double measure_fp_div_latency() {
    constexpr int CHAIN = 50000;
    return measure_cycles_per_op([&]() {
        double val = 1e18;
        for (int i = 0; i < CHAIN; i++) {
            val = val / 1.0000001;
            clobber();
        }
        escape(val);
    }, CHAIN, 20, 2);
}

// ============================================================
// Instruction Throughput — independent operations saturate units
// ============================================================

// Throughput: NO clobber() inside loop — we WANT the compiler to see
// these as independent, and we WANT the CPU to execute them in parallel.
// escape() at the end prevents dead code elimination.
// Use very large iteration count so total time >> timer resolution.

static double measure_int_add_throughput() {
    constexpr int OPS_PER_ITER = 8;
    constexpr int ITERS = 500000;
    constexpr int TOTAL = OPS_PER_ITER * ITERS;
    return measure_cycles_per_op([&]() {
        uint64_t a=1, b=2, c=3, d=4, e=5, f=6, g=7, h=8;
        for (int i = 0; i < ITERS; i++) {
            a += i; b += i; c += i; d += i;  // use i to prevent strength reduction
            e += i; f += i; g += i; h += i;
        }
        escape(a); escape(b); escape(c); escape(d);
        escape(e); escape(f); escape(g); escape(h);
    }, TOTAL, 10, 2);
}

static double measure_int_mul_throughput() {
    constexpr int OPS_PER_ITER = 8;
    constexpr int ITERS = 500000;
    constexpr int TOTAL = OPS_PER_ITER * ITERS;
    return measure_cycles_per_op([&]() {
        uint64_t a=1, b=2, c=3, d=4, e=5, f=6, g=7, h=8;
        for (int i = 0; i < ITERS; i++) {
            a *= 3; b *= 3; c *= 3; d *= 3;
            e *= 3; f *= 3; g *= 3; h *= 3;
        }
        escape(a); escape(b); escape(c); escape(d);
        escape(e); escape(f); escape(g); escape(h);
    }, TOTAL, 10, 2);
}

static double measure_fp_add_throughput() {
    constexpr int OPS_PER_ITER = 8;
    constexpr int ITERS = 500000;
    constexpr int TOTAL = OPS_PER_ITER * ITERS;
    return measure_cycles_per_op([&]() {
        double a=1, b=2, c=3, d=4, e=5, f=6, g=7, h=8;
        double inc = 0.5;
        escape(inc); // prevent constant propagation
        for (int i = 0; i < ITERS; i++) {
            a += inc; b += inc; c += inc; d += inc;
            e += inc; f += inc; g += inc; h += inc;
        }
        escape(a); escape(b); escape(c); escape(d);
        escape(e); escape(f); escape(g); escape(h);
    }, TOTAL, 10, 2);
}

static double measure_fp_mul_throughput() {
    constexpr int OPS_PER_ITER = 8;
    constexpr int ITERS = 500000;
    constexpr int TOTAL = OPS_PER_ITER * ITERS;
    return measure_cycles_per_op([&]() {
        double a=1.0001, b=1.0001, c=1.0001, d=1.0001;
        double e=1.0001, f=1.0001, g=1.0001, h=1.0001;
        double k = 1.0000001;
        escape(k);
        for (int i = 0; i < ITERS; i++) {
            a *= k; b *= k; c *= k; d *= k;
            e *= k; f *= k; g *= k; h *= k;
        }
        escape(a); escape(b); escape(c); escape(d);
        escape(e); escape(f); escape(g); escape(h);
    }, TOTAL, 10, 2);
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
