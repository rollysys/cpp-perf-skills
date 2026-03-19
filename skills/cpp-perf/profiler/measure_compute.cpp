#include "common.h"
#include <cstdint>

namespace profiler {

// ============================================================
// Instruction Latency — dependent chains force serial execution
// ============================================================

static double measure_int_add_latency() {
    constexpr int CHAIN = 200;
    return measure_cycles_per_op([&]() {
        uint64_t val = 1;
#if defined(__aarch64__)
        asm volatile(
            ".rept 200\n"
            "add %0, %0, #1\n"
            ".endr\n"
            : "+r"(val)
        );
#elif defined(__x86_64__)
        asm volatile(
            ".rept 200\n"
            "addq $1, %0\n"
            ".endr\n"
            : "+r"(val)
        );
#endif
        escape(val);
    }, CHAIN, 500, 50);
}

static double measure_int_mul_latency() {
    constexpr int CHAIN = 200;
    return measure_cycles_per_op([&]() {
        uint64_t val = 7;
#if defined(__aarch64__)
        // mul xD, xN, xM — need a second register with a constant multiplier
        uint64_t three = 3;
        asm volatile(
            ".rept 200\n"
            "mul %0, %0, %1\n"
            ".endr\n"
            : "+r"(val)
            : "r"(three)
        );
#elif defined(__x86_64__)
        // imul rax, rax, 3 — three-operand form, result depends on source
        asm volatile(
            ".rept 200\n"
            "imulq $3, %0, %0\n"
            ".endr\n"
            : "+r"(val)
        );
#endif
        escape(val);
    }, CHAIN, 500, 50);
}

static double measure_int_div_latency() {
    constexpr int CHAIN = 100;
    return measure_cycles_per_op([&]() {
#if defined(__aarch64__)
        uint64_t val = 0x7FFFFFFFFFFFFFFFULL;
        uint64_t divisor = 7;
        asm volatile(
            ".rept 100\n"
            "udiv %0, %0, %1\n"
            ".endr\n"
            : "+r"(val)
            : "r"(divisor)
        );
        escape(val);
#elif defined(__x86_64__)
        // x86 div uses rdx:rax / operand -> rax (quot), rdx (rem)
        // Chain: result of previous div (in rax) feeds next div
        uint64_t val = 0x7FFFFFFFFFFFFFFFULL;
        uint64_t divisor = 7;
        // We need rdx=0 before each div. Use a simpler approach:
        // repeatedly divide, using the quotient as next dividend.
        // Since x86 div clobbers rdx, we must zero it each iteration.
        asm volatile(
            "movq %0, %%rax\n"
            ".rept 100\n"
            "xorq %%rdx, %%rdx\n"
            "divq %1\n"
            ".endr\n"
            "movq %%rax, %0\n"
            : "+r"(val)
            : "r"(divisor)
            : "rax", "rdx"
        );
        escape(val);
#endif
    }, CHAIN, 200, 20);
}

static double measure_fp_add_latency() {
    constexpr int CHAIN = 200;
    return measure_cycles_per_op([&]() {
        double val = 1.0;
#if defined(__aarch64__)
        asm volatile(
            "fmov d0, %x0\n"
            ".rept 200\n"
            "fadd d0, d0, d0\n"
            ".endr\n"
            "fmov %x0, d0\n"
            : "+r"(val)
            :
            : "d0"
        );
#elif defined(__x86_64__)
        asm volatile(
            "movsd %0, %%xmm0\n"
            ".rept 200\n"
            "addsd %%xmm0, %%xmm0\n"
            ".endr\n"
            "movsd %%xmm0, %0\n"
            : "+m"(val)
            :
            : "xmm0"
        );
#endif
        escape(val);
    }, CHAIN, 500, 50);
}

static double measure_fp_mul_latency() {
    constexpr int CHAIN = 200;
    return measure_cycles_per_op([&]() {
        double val = 1.0000001;
#if defined(__aarch64__)
        asm volatile(
            "fmov d0, %x0\n"
            ".rept 200\n"
            "fmul d0, d0, d0\n"
            ".endr\n"
            "fmov %x0, d0\n"
            : "+r"(val)
            :
            : "d0"
        );
#elif defined(__x86_64__)
        asm volatile(
            "movsd %0, %%xmm0\n"
            ".rept 200\n"
            "mulsd %%xmm0, %%xmm0\n"
            ".endr\n"
            "movsd %%xmm0, %0\n"
            : "+m"(val)
            :
            : "xmm0"
        );
#endif
        escape(val);
    }, CHAIN, 500, 50);
}

static double measure_fp_div_latency() {
    constexpr int CHAIN = 100;
    return measure_cycles_per_op([&]() {
#if defined(__aarch64__)
        double val = 1e18;
        double divisor = 1.0000001;
        asm volatile(
            "fmov d0, %x0\n"
            "fmov d1, %x1\n"
            ".rept 100\n"
            "fdiv d0, d0, d1\n"
            ".endr\n"
            "fmov %x0, d0\n"
            : "+r"(val)
            : "r"(divisor)
            : "d0", "d1"
        );
        escape(val);
#elif defined(__x86_64__)
        double val = 1e18;
        double divisor = 1.0000001;
        asm volatile(
            "movsd %0, %%xmm0\n"
            "movsd %1, %%xmm1\n"
            ".rept 100\n"
            "divsd %%xmm1, %%xmm0\n"
            ".endr\n"
            "movsd %%xmm0, %0\n"
            : "+m"(val)
            : "m"(divisor)
            : "xmm0", "xmm1"
        );
        escape(val);
#endif
    }, CHAIN, 200, 20);
}

// ============================================================
// Instruction Throughput — independent operations saturate units
// ============================================================

static double measure_int_add_throughput() {
    // 8 independent adds per iteration, 200 unrolled iterations = 1600 ops
    constexpr int OPS = 1600;
    return measure_cycles_per_op([&]() {
        uint64_t a=1, b=2, c=3, d=4, e=5, f=6, g=7, h=8;
#if defined(__aarch64__)
        asm volatile(
            ".rept 200\n"
            "add %0, %0, #1\n"
            "add %1, %1, #1\n"
            "add %2, %2, #1\n"
            "add %3, %3, #1\n"
            "add %4, %4, #1\n"
            "add %5, %5, #1\n"
            "add %6, %6, #1\n"
            "add %7, %7, #1\n"
            ".endr\n"
            : "+r"(a), "+r"(b), "+r"(c), "+r"(d),
              "+r"(e), "+r"(f), "+r"(g), "+r"(h)
        );
#elif defined(__x86_64__)
        asm volatile(
            ".rept 200\n"
            "addq $1, %0\n"
            "addq $1, %1\n"
            "addq $1, %2\n"
            "addq $1, %3\n"
            "addq $1, %4\n"
            "addq $1, %5\n"
            "addq $1, %6\n"
            "addq $1, %7\n"
            ".endr\n"
            : "+r"(a), "+r"(b), "+r"(c), "+r"(d),
              "+r"(e), "+r"(f), "+r"(g), "+r"(h)
        );
#endif
        escape(a); escape(b); escape(c); escape(d);
        escape(e); escape(f); escape(g); escape(h);
    }, OPS, 300, 30);
}

static double measure_int_mul_throughput() {
    constexpr int OPS = 1600;
    return measure_cycles_per_op([&]() {
        uint64_t a=1, b=2, c=3, d=4, e=5, f=6, g=7, h=8;
        uint64_t k = 3;
#if defined(__aarch64__)
        asm volatile(
            ".rept 200\n"
            "mul %0, %0, %8\n"
            "mul %1, %1, %8\n"
            "mul %2, %2, %8\n"
            "mul %3, %3, %8\n"
            "mul %4, %4, %8\n"
            "mul %5, %5, %8\n"
            "mul %6, %6, %8\n"
            "mul %7, %7, %8\n"
            ".endr\n"
            : "+r"(a), "+r"(b), "+r"(c), "+r"(d),
              "+r"(e), "+r"(f), "+r"(g), "+r"(h)
            : "r"(k)
        );
#elif defined(__x86_64__)
        asm volatile(
            ".rept 200\n"
            "imulq $3, %0, %0\n"
            "imulq $3, %1, %1\n"
            "imulq $3, %2, %2\n"
            "imulq $3, %3, %3\n"
            "imulq $3, %4, %4\n"
            "imulq $3, %5, %5\n"
            "imulq $3, %6, %6\n"
            "imulq $3, %7, %7\n"
            ".endr\n"
            : "+r"(a), "+r"(b), "+r"(c), "+r"(d),
              "+r"(e), "+r"(f), "+r"(g), "+r"(h)
        );
#endif
        escape(a); escape(b); escape(c); escape(d);
        escape(e); escape(f); escape(g); escape(h);
    }, OPS, 300, 30);
}

static double measure_fp_add_throughput() {
    constexpr int OPS = 800;  // 8 independent adds x 100 iterations
    return measure_cycles_per_op([&]() {
#if defined(__aarch64__)
        asm volatile(
            "fmov d0, #1.0\n"
            "fmov d1, #2.0\n"
            "fmov d2, #3.0\n"
            "fmov d3, #4.0\n"
            "fmov d4, #5.0\n"
            "fmov d5, #6.0\n"
            "fmov d6, #7.0\n"
            "fmov d7, #8.0\n"
            ".rept 100\n"
            "fadd d0, d0, d0\n"
            "fadd d1, d1, d1\n"
            "fadd d2, d2, d2\n"
            "fadd d3, d3, d3\n"
            "fadd d4, d4, d4\n"
            "fadd d5, d5, d5\n"
            "fadd d6, d6, d6\n"
            "fadd d7, d7, d7\n"
            ".endr\n"
            ::: "d0","d1","d2","d3","d4","d5","d6","d7"
        );
#elif defined(__x86_64__)
        asm volatile(
            "xorpd %%xmm0, %%xmm0\n"
            "xorpd %%xmm1, %%xmm1\n"
            "xorpd %%xmm2, %%xmm2\n"
            "xorpd %%xmm3, %%xmm3\n"
            "xorpd %%xmm4, %%xmm4\n"
            "xorpd %%xmm5, %%xmm5\n"
            "xorpd %%xmm6, %%xmm6\n"
            "xorpd %%xmm7, %%xmm7\n"
            ".rept 100\n"
            "addsd %%xmm0, %%xmm0\n"
            "addsd %%xmm1, %%xmm1\n"
            "addsd %%xmm2, %%xmm2\n"
            "addsd %%xmm3, %%xmm3\n"
            "addsd %%xmm4, %%xmm4\n"
            "addsd %%xmm5, %%xmm5\n"
            "addsd %%xmm6, %%xmm6\n"
            "addsd %%xmm7, %%xmm7\n"
            ".endr\n"
            ::: "xmm0","xmm1","xmm2","xmm3","xmm4","xmm5","xmm6","xmm7"
        );
#endif
    }, OPS, 300, 30);
}

static double measure_fp_mul_throughput() {
    constexpr int OPS = 800;
    return measure_cycles_per_op([&]() {
#if defined(__aarch64__)
        asm volatile(
            "fmov d0, #1.0\n"
            "fmov d1, #1.0\n"
            "fmov d2, #1.0\n"
            "fmov d3, #1.0\n"
            "fmov d4, #1.0\n"
            "fmov d5, #1.0\n"
            "fmov d6, #1.0\n"
            "fmov d7, #1.0\n"
            ".rept 100\n"
            "fmul d0, d0, d0\n"
            "fmul d1, d1, d1\n"
            "fmul d2, d2, d2\n"
            "fmul d3, d3, d3\n"
            "fmul d4, d4, d4\n"
            "fmul d5, d5, d5\n"
            "fmul d6, d6, d6\n"
            "fmul d7, d7, d7\n"
            ".endr\n"
            ::: "d0","d1","d2","d3","d4","d5","d6","d7"
        );
#elif defined(__x86_64__)
        asm volatile(
            "xorpd %%xmm0, %%xmm0\n"
            "xorpd %%xmm1, %%xmm1\n"
            "xorpd %%xmm2, %%xmm2\n"
            "xorpd %%xmm3, %%xmm3\n"
            "xorpd %%xmm4, %%xmm4\n"
            "xorpd %%xmm5, %%xmm5\n"
            "xorpd %%xmm6, %%xmm6\n"
            "xorpd %%xmm7, %%xmm7\n"
            ".rept 100\n"
            "mulsd %%xmm0, %%xmm0\n"
            "mulsd %%xmm1, %%xmm1\n"
            "mulsd %%xmm2, %%xmm2\n"
            "mulsd %%xmm3, %%xmm3\n"
            "mulsd %%xmm4, %%xmm4\n"
            "mulsd %%xmm5, %%xmm5\n"
            "mulsd %%xmm6, %%xmm6\n"
            "mulsd %%xmm7, %%xmm7\n"
            ".endr\n"
            ::: "xmm0","xmm1","xmm2","xmm3","xmm4","xmm5","xmm6","xmm7"
        );
#endif
    }, OPS, 300, 30);
}

// NEON/AVX SIMD throughput — vector multiply (4x float32)
// These give a sense of the vector unit throughput.
// On aarch64: fmul v0.4s, v0.4s, v1.4s (NEON 128-bit, 4 floats)
// On x86_64:  vmulps xmm (SSE) or ymm (AVX) — use SSE for broader compatibility
static double measure_simd_mul_throughput() {
    constexpr int OPS = 800; // 8 independent SIMD muls x 100 iterations
    return measure_cycles_per_op([&]() {
#if defined(__aarch64__)
        asm volatile(
            "movi v0.4s, #0\n"
            "movi v1.4s, #0\n"
            "movi v2.4s, #0\n"
            "movi v3.4s, #0\n"
            "movi v4.4s, #0\n"
            "movi v5.4s, #0\n"
            "movi v6.4s, #0\n"
            "movi v7.4s, #0\n"
            ".rept 100\n"
            "fmul v0.4s, v0.4s, v0.4s\n"
            "fmul v1.4s, v1.4s, v1.4s\n"
            "fmul v2.4s, v2.4s, v2.4s\n"
            "fmul v3.4s, v3.4s, v3.4s\n"
            "fmul v4.4s, v4.4s, v4.4s\n"
            "fmul v5.4s, v5.4s, v5.4s\n"
            "fmul v6.4s, v6.4s, v6.4s\n"
            "fmul v7.4s, v7.4s, v7.4s\n"
            ".endr\n"
            ::: "v0","v1","v2","v3","v4","v5","v6","v7"
        );
#elif defined(__x86_64__)
        // SSE mulps — 4 floats at a time
        asm volatile(
            "xorps %%xmm0, %%xmm0\n"
            "xorps %%xmm1, %%xmm1\n"
            "xorps %%xmm2, %%xmm2\n"
            "xorps %%xmm3, %%xmm3\n"
            "xorps %%xmm4, %%xmm4\n"
            "xorps %%xmm5, %%xmm5\n"
            "xorps %%xmm6, %%xmm6\n"
            "xorps %%xmm7, %%xmm7\n"
            ".rept 100\n"
            "mulps %%xmm0, %%xmm0\n"
            "mulps %%xmm1, %%xmm1\n"
            "mulps %%xmm2, %%xmm2\n"
            "mulps %%xmm3, %%xmm3\n"
            "mulps %%xmm4, %%xmm4\n"
            "mulps %%xmm5, %%xmm5\n"
            "mulps %%xmm6, %%xmm6\n"
            "mulps %%xmm7, %%xmm7\n"
            ".endr\n"
            ::: "xmm0","xmm1","xmm2","xmm3","xmm4","xmm5","xmm6","xmm7"
        );
#endif
    }, OPS, 300, 30);
}

// ============================================================
// Entry point
// ============================================================

void measure_compute() {
    // --- Latency ---
    record("instructions.integer", "add_lat", measure_int_add_latency());
    record("instructions.integer", "mul_lat", measure_int_mul_latency());
    record("instructions.integer", "div_lat", measure_int_div_latency());
    record("instructions.fp",      "fadd_lat", measure_fp_add_latency());
    record("instructions.fp",      "fmul_lat", measure_fp_mul_latency());
    record("instructions.fp",      "fdiv_lat", measure_fp_div_latency());

    // --- Throughput ---
    record("instructions.integer", "add_tp", measure_int_add_throughput());
    record("instructions.integer", "mul_tp", measure_int_mul_throughput());
    record("instructions.fp",      "fadd_tp", measure_fp_add_throughput());
    record("instructions.fp",      "fmul_tp", measure_fp_mul_throughput());

    // --- SIMD throughput (NEON 4xf32 / SSE mulps 4xf32) ---
    record("instructions.simd",    "vmul_4xf32_tp", measure_simd_mul_throughput());
}

} // namespace profiler
