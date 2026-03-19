#include "common.h"
#include <cstdint>
#include <cmath>

#if defined(__aarch64__)
#include <arm_neon.h>
#endif

#if defined(__x86_64__)
#include <immintrin.h>
#endif

namespace profiler {

// ============================================================
// Helper: latency measurement (dependency chain with clobber)
// ============================================================
template <typename T, typename Op>
static double lat(Op op, int chain = 100000, int iters = 30, int warmup = 3) {
    return measure_ns_per_op([&]() {
        T val = op.init();
        for (int i = 0; i < chain; i++) {
            val = op(val);
            clobber();
        }
        escape(val);
    }, chain, iters, warmup);
}

// ============================================================
// Helper: throughput measurement (8 independent streams, no clobber)
// ============================================================
template <typename T, typename Op>
static double tp(Op op, int iters = 200000, int runs = 15, int warmup = 2) {
    return measure_ns_per_op([&]() {
        T a = op.init(), b = op.init(), c = op.init(), d = op.init();
        T e = op.init(), f = op.init(), g = op.init(), h = op.init();
        for (int i = 0; i < iters; i++) {
            a = op(a); b = op(b); c = op(c); d = op(d);
            e = op(e); f = op(f); g = op(g); h = op(h);
        }
        escape(a); escape(b); escape(c); escape(d);
        escape(e); escape(f); escape(g); escape(h);
    }, 8 * iters, runs, warmup);
}

// ============================================================
// Integer operations
// ============================================================
struct IntAdd  { uint64_t init() { return 1; } uint64_t operator()(uint64_t v) { return v + 7; } };
struct IntSub  { uint64_t init() { return UINT64_MAX; } uint64_t operator()(uint64_t v) { return v - 3; } };
struct IntMul  { uint64_t init() { return 7; } uint64_t operator()(uint64_t v) { return v * 3; } };
struct IntDiv  { uint64_t init() { return UINT64_MAX/2; } uint64_t operator()(uint64_t v) { return v / 7 + 1; } };
struct IntAnd  { uint64_t init() { return 0xDEADBEEFCAFEBABEULL; } uint64_t operator()(uint64_t v) { return v & (v >> 1); } };
struct IntOr   { uint64_t init() { return 1; } uint64_t operator()(uint64_t v) { return v | (v + 1); } };
struct IntXor  { uint64_t init() { return 0xDEADBEEFULL; } uint64_t operator()(uint64_t v) { return v ^ (v >> 1); } };
struct IntShiftL { uint64_t init() { return 1; } uint64_t operator()(uint64_t v) { return (v << 1) | 1; } };
struct IntShiftR { uint64_t init() { return UINT64_MAX; } uint64_t operator()(uint64_t v) { return (v >> 1) | (1ULL << 63); } };
struct IntCsel { uint64_t init() { return 1; }
    uint64_t operator()(uint64_t v) { return (v & 1) ? v + 1 : v + 2; } };
struct IntClz  { uint64_t init() { return 0xDEADBEEFULL; }
    uint64_t operator()(uint64_t v) { return v ^ __builtin_clzll(v); } };
struct IntPopcnt { uint64_t init() { return 0xDEADBEEFCAFEBABEULL; }
    uint64_t operator()(uint64_t v) { return v ^ __builtin_popcountll(v); } };

// ============================================================
// FP scalar operations
// ============================================================
struct FpAdd   { double init() { return 1.0; } double operator()(double v) { return v + 0.5; } };
struct FpSub   { double init() { return 1e18; } double operator()(double v) { return v - 0.5; } };
struct FpMul   { double init() { return 1.000001; } double operator()(double v) { return v * 1.000001; } };
struct FpDiv   { double init() { return 1e18; } double operator()(double v) { return v / 1.000001; } };
struct FpSqrt  { double init() { return 1e18; } double operator()(double v) { return std::sqrt(v) + 1.0; } };
struct FpAbs   { double init() { return -1.5; } double operator()(double v) { return std::fabs(v) - 3.0; } };
struct FpFma   { double init() { return 1.0; } double operator()(double v) { return std::fma(v, 1.000001, 0.5); } };
struct FpCvtI2F { double init() { return 0; }
    double operator()(double v) { return (double)((int64_t)v + 1); } };
struct FpCvtF2I { double init() { return 1e8; }
    double operator()(double v) { return (double)((int64_t)(v * 0.999999) + 1); } };

// Float32 versions for comparison
struct FpAdd32 { float init() { return 1.0f; } float operator()(float v) { return v + 0.5f; } };
struct FpMul32 { float init() { return 1.00001f; } float operator()(float v) { return v * 1.00001f; } };
struct FpDiv32 { float init() { return 1e18f; } float operator()(float v) { return v / 1.00001f; } };
struct FpFma32 { float init() { return 1.0f; } float operator()(float v) { return std::fma(v, 1.00001f, 0.5f); } };

// ============================================================
// NEON / SSE vector operations (4 x float32)
// ============================================================
#if defined(__aarch64__)
using vf32 = float32x4_t;
struct VecAdd { vf32 init() { return vdupq_n_f32(1.0f); }
    vf32 operator()(vf32 v) { return vaddq_f32(v, vdupq_n_f32(0.5f)); } };
struct VecMul { vf32 init() { return vdupq_n_f32(1.00001f); }
    vf32 operator()(vf32 v) { return vmulq_f32(v, vdupq_n_f32(1.00001f)); } };
struct VecFma { vf32 init() { return vdupq_n_f32(1.0f); }
    vf32 operator()(vf32 v) { return vfmaq_f32(v, v, vdupq_n_f32(0.00001f)); } };
struct VecAbs { vf32 init() { return vdupq_n_f32(-1.5f); }
    vf32 operator()(vf32 v) { return vabsq_f32(vsubq_f32(v, vdupq_n_f32(3.0f))); } };
struct VecMin { vf32 init() { return vdupq_n_f32(100.0f); }
    vf32 operator()(vf32 v) { return vminq_f32(v, vsubq_f32(v, vdupq_n_f32(0.1f))); } };
struct VecCvt { vf32 init() { return vdupq_n_f32(1.5f); }
    vf32 operator()(vf32 v) { return vcvtq_f32_s32(vcvtq_s32_f32(vaddq_f32(v, vdupq_n_f32(0.1f)))); } };

// Load/Store throughput
static double measure_vec_load_tp() {
    constexpr int N = 1024 * 256;  // 1MB of floats
    static float data[N] __attribute__((aligned(64)));
    for (int i = 0; i < N; i++) data[i] = (float)i;
    constexpr int OPS = N / 4;  // 4 floats per vector load
    return measure_ns_per_op([&]() {
        vf32 sum = vdupq_n_f32(0);
        for (int i = 0; i < N; i += 4) {
            sum = vaddq_f32(sum, vld1q_f32(&data[i]));
        }
        escape(sum);
    }, OPS, 20, 3);
}

static double measure_vec_store_tp() {
    constexpr int N = 1024 * 256;
    static float data[N] __attribute__((aligned(64)));
    constexpr int OPS = N / 4;
    return measure_ns_per_op([&]() {
        vf32 val = vdupq_n_f32(42.0f);
        for (int i = 0; i < N; i += 4) {
            vst1q_f32(&data[i], val);
        }
        clobber();
    }, OPS, 20, 3);
}

#elif defined(__x86_64__)
using vf32 = __m128;
struct VecAdd { vf32 init() { return _mm_set1_ps(1.0f); }
    vf32 operator()(vf32 v) { return _mm_add_ps(v, _mm_set1_ps(0.5f)); } };
struct VecMul { vf32 init() { return _mm_set1_ps(1.00001f); }
    vf32 operator()(vf32 v) { return _mm_mul_ps(v, _mm_set1_ps(1.00001f)); } };
struct VecFma { vf32 init() { return _mm_set1_ps(1.0f); }
    vf32 operator()(vf32 v) { return _mm_fmadd_ps(v, v, _mm_set1_ps(0.00001f)); } };
struct VecAbs { vf32 init() { return _mm_set1_ps(-1.5f); }
    vf32 operator()(vf32 v) {
        return _mm_andnot_ps(_mm_set1_ps(-0.0f), _mm_sub_ps(v, _mm_set1_ps(3.0f))); } };
struct VecMin { vf32 init() { return _mm_set1_ps(100.0f); }
    vf32 operator()(vf32 v) { return _mm_min_ps(v, _mm_sub_ps(v, _mm_set1_ps(0.1f))); } };
struct VecCvt { vf32 init() { return _mm_set1_ps(1.5f); }
    vf32 operator()(vf32 v) { return _mm_cvtepi32_ps(_mm_cvtps_epi32(_mm_add_ps(v, _mm_set1_ps(0.1f)))); } };

static double measure_vec_load_tp() {
    constexpr int N = 1024 * 256;
    static float data[N] __attribute__((aligned(64)));
    for (int i = 0; i < N; i++) data[i] = (float)i;
    constexpr int OPS = N / 4;
    return measure_ns_per_op([&]() {
        __m128 sum = _mm_setzero_ps();
        for (int i = 0; i < N; i += 4)
            sum = _mm_add_ps(sum, _mm_load_ps(&data[i]));
        escape(sum);
    }, OPS, 20, 3);
}

static double measure_vec_store_tp() {
    constexpr int N = 1024 * 256;
    static float data[N] __attribute__((aligned(64)));
    constexpr int OPS = N / 4;
    return measure_ns_per_op([&]() {
        __m128 val = _mm_set1_ps(42.0f);
        for (int i = 0; i < N; i += 4)
            _mm_store_ps(&data[i], val);
        clobber();
    }, OPS, 20, 3);
}
#endif

// ============================================================
// Memory load/store latency and throughput
// ============================================================
static double measure_scalar_load_tp() {
    constexpr int N = 1024 * 1024;  // 4MB (fits in L2/L3)
    static int32_t data[N];
    for (int i = 0; i < N; i++) data[i] = i;
    return measure_ns_per_op([&]() {
        int32_t sum = 0;
        for (int i = 0; i < N; i++) sum += data[i];
        escape(sum);
    }, N, 20, 3);
}

static double measure_scalar_store_tp() {
    constexpr int N = 1024 * 1024;
    static int32_t data[N];
    return measure_ns_per_op([&]() {
        for (int i = 0; i < N; i++) data[i] = i;
        clobber();
    }, N, 20, 3);
}

// ============================================================
// Entry point
// ============================================================

void measure_compute() {
    fprintf(stderr, "  integer latency...\n");
    record("instructions.integer", "add_lat",    lat<uint64_t>(IntAdd{}));
    record("instructions.integer", "sub_lat",    lat<uint64_t>(IntSub{}));
    record("instructions.integer", "mul_lat",    lat<uint64_t>(IntMul{}));
    record("instructions.integer", "div_lat",    lat<uint64_t>(IntDiv{}, 50000));
    record("instructions.integer", "and_lat",    lat<uint64_t>(IntAnd{}));
    record("instructions.integer", "or_lat",     lat<uint64_t>(IntOr{}));
    record("instructions.integer", "xor_lat",    lat<uint64_t>(IntXor{}));
    record("instructions.integer", "shl_lat",    lat<uint64_t>(IntShiftL{}));
    record("instructions.integer", "shr_lat",    lat<uint64_t>(IntShiftR{}));
    record("instructions.integer", "csel_lat",   lat<uint64_t>(IntCsel{}));
    record("instructions.integer", "clz_lat",    lat<uint64_t>(IntClz{}));
    record("instructions.integer", "popcnt_lat", lat<uint64_t>(IntPopcnt{}));

    fprintf(stderr, "  integer throughput...\n");
    record("instructions.integer", "add_tp",     tp<uint64_t>(IntAdd{}));
    record("instructions.integer", "mul_tp",     tp<uint64_t>(IntMul{}));

    fprintf(stderr, "  fp64 latency...\n");
    record("instructions.fp64", "add_lat",    lat<double>(FpAdd{}));
    record("instructions.fp64", "sub_lat",    lat<double>(FpSub{}));
    record("instructions.fp64", "mul_lat",    lat<double>(FpMul{}));
    record("instructions.fp64", "div_lat",    lat<double>(FpDiv{}, 50000));
    record("instructions.fp64", "sqrt_lat",   lat<double>(FpSqrt{}, 50000));
    record("instructions.fp64", "abs_lat",    lat<double>(FpAbs{}));
    record("instructions.fp64", "fma_lat",    lat<double>(FpFma{}));
    record("instructions.fp64", "cvt_i2f_lat", lat<double>(FpCvtI2F{}));
    record("instructions.fp64", "cvt_f2i_lat", lat<double>(FpCvtF2I{}));

    fprintf(stderr, "  fp64 throughput...\n");
    record("instructions.fp64", "add_tp",     tp<double>(FpAdd{}));
    record("instructions.fp64", "mul_tp",     tp<double>(FpMul{}));
    record("instructions.fp64", "fma_tp",     tp<double>(FpFma{}));

    fprintf(stderr, "  fp32 latency...\n");
    record("instructions.fp32", "add_lat",    lat<float>(FpAdd32{}));
    record("instructions.fp32", "mul_lat",    lat<float>(FpMul32{}));
    record("instructions.fp32", "div_lat",    lat<float>(FpDiv32{}, 50000));
    record("instructions.fp32", "fma_lat",    lat<float>(FpFma32{}));

    fprintf(stderr, "  fp32 throughput...\n");
    record("instructions.fp32", "add_tp",     tp<float>(FpAdd32{}));
    record("instructions.fp32", "mul_tp",     tp<float>(FpMul32{}));
    record("instructions.fp32", "fma_tp",     tp<float>(FpFma32{}));

    fprintf(stderr, "  SIMD (4xf32) latency...\n");
    record("instructions.simd_4xf32", "add_lat",  lat<vf32>(VecAdd{}));
    record("instructions.simd_4xf32", "mul_lat",  lat<vf32>(VecMul{}));
    record("instructions.simd_4xf32", "fma_lat",  lat<vf32>(VecFma{}));
    record("instructions.simd_4xf32", "abs_lat",  lat<vf32>(VecAbs{}));
    record("instructions.simd_4xf32", "min_lat",  lat<vf32>(VecMin{}));
    record("instructions.simd_4xf32", "cvt_lat",  lat<vf32>(VecCvt{}));

    fprintf(stderr, "  SIMD (4xf32) throughput...\n");
    record("instructions.simd_4xf32", "add_tp",   tp<vf32>(VecAdd{}));
    record("instructions.simd_4xf32", "mul_tp",   tp<vf32>(VecMul{}));
    record("instructions.simd_4xf32", "fma_tp",   tp<vf32>(VecFma{}));
    record("instructions.simd_4xf32", "load_tp",  measure_vec_load_tp());
    record("instructions.simd_4xf32", "store_tp", measure_vec_store_tp());

    fprintf(stderr, "  memory scalar...\n");
    record("instructions.memory", "load_tp",   measure_scalar_load_tp());
    record("instructions.memory", "store_tp",  measure_scalar_store_tp());
}

} // namespace profiler
