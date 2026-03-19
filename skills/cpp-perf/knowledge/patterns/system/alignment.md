---
name: Memory Alignment for Performance
source: perf-book Ch.8, perf-ninja memory_bound/mem_alignment_1, Cpp-High-Performance Ch.7
layers: [system, microarchitecture]
platforms: [arm, x86]
keywords: [alignment, alignas, aligned_alloc, cache line, 64-byte, split load, SIMD alignment, posix_memalign]
---

## Problem

When data is not aligned to cache line or SIMD register boundaries, loads and stores that cross a cache line boundary become *split loads/stores*. A split access requires reading two cache lines and combining the parts in a limited number of split registers. When this happens sporadically, the penalty is negligible. When it happens on every iteration of a hot loop (e.g., SIMD-vectorized matrix multiplication), performance degrades severely.

The perf-ninja `mem_alignment_1` lab demonstrates this with matrix multiplication. When matrix rows are not aligned to cache line boundaries, every SIMD load in the inner loop can trigger a split load:

```cpp
// Problem setup: N x N matrix stored as flat array
// If N is not a multiple of cache-line-width / sizeof(float),
// rows start at misaligned offsets
using Matrix = std::vector<float>;  // default allocator, no alignment guarantee

// Inner loop of blocked GEMM -- every access to B[k*K+j] and C[i*K+j]
// may cross a cache line boundary if K is not aligned
for (int j = jj; j < std::min(jj + blockSize, N); ++j)
  C[i * K + j] += A[i * K + k] * B[k * K + j];  // split loads if misaligned
```

Two alignment issues must be solved simultaneously:
1. **Array base address:** the start of the matrix must be aligned to cache line boundary
2. **Row stride:** each row must start at an aligned offset, which may require padding columns

## Detection

**Source-level indicators:**
- Arrays or vectors of floats/doubles used in SIMD-vectorized loops without alignment specifications
- Matrix dimensions that are not multiples of cache line size / element size (e.g., 63 floats = 252 bytes, not a multiple of 64)
- Use of `std::vector` with default allocator for data processed by SIMD intrinsics
- Structs containing SIMD types (`__m256`, `float32x4_t`) without `alignas`

**Profile-level indicators:**
- x86 TMA: `Memory_Bound > L1_Bound > Split Loads` category
- `perf stat -e mem_inst_retired.split_loads,mem_inst_retired.split_stores`: high split load/store counts
- On ARM: unaligned access faults or performance counters for unaligned accesses
- Poor performance at specific matrix sizes (e.g., powers of 2) but not at N+1 or N-1

**Characteristic symptom:** performance varies wildly with tiny changes in array dimensions (e.g., 512x512 is fast, 513x513 is fast, but 511x511 is slow due to misaligned rows).

## Transformation

### Strategy 1: Cache-line aligned allocator for the container

Replace `std::vector` with a vector using a cache-line aligned allocator. From the perf-ninja solution:

```cpp
// Before: default allocator, no alignment guarantee
using Matrix = std::vector<float>;

// After: custom allocator ensuring cache-line alignment
template <typename T>
class CacheLineAlignedAllocator {
public:
  using value_type = T;
  // 64 bytes on x86/most ARM, 128 bytes on Apple Silicon L2
  static std::align_val_t constexpr ALIGNMENT{64};

  [[nodiscard]] T* allocate(std::size_t N) {
    return reinterpret_cast<T*>(
        ::operator new[](N * sizeof(T), ALIGNMENT));
  }
  void deallocate(T* allocPtr, [[maybe_unused]] std::size_t N) {
    ::operator delete[](allocPtr, ALIGNMENT);
  }
};

template<typename T>
using AlignedVector = std::vector<T, CacheLineAlignedAllocator<T>>;
using Matrix = AlignedVector<float>;  // base address is now cache-line aligned
```

### Strategy 2: Pad rows to aligned stride

Even with an aligned base address, row `i` starts at offset `i * K * sizeof(float)`. If `K * sizeof(float)` is not a multiple of the cache line size, interior rows are misaligned. Pad `K` to the next aligned boundary:

```cpp
// Before: K == N, rows may start at unaligned offsets
int n_columns(int N) {
  return N;
}

// After: pad K so each row starts at a cache-line-aligned offset
// For 64-byte cache lines with 4-byte floats: align to multiple of 16
int n_columns(int N) {
  constexpr int floats_per_cacheline = 64 / sizeof(float);  // 16
  return (N + floats_per_cacheline - 1) / floats_per_cacheline
         * floats_per_cacheline;
}
```

For Apple Silicon (128-byte L2 cache lines), use 32 floats per cache line instead.

### Strategy 3: alignas for stack and struct alignment

Use C++11 `alignas` for stack-allocated arrays and struct members:

```cpp
// Stack-allocated aligned array
alignas(64) float buffer[1024];

// Aligned struct for SIMD processing
struct alignas(64) SimdFriendlyData {
  float values[16];  // exactly one cache line
};
```

Cpp-High-Performance Chapter 7 confirms that `new` returns memory aligned for `std::max_align_t` (typically 16 bytes), but this is insufficient for AVX (32 bytes), AVX-512 (64 bytes), or cache-line alignment:

```cpp
// Standard new only guarantees alignof(std::max_align_t) == 16
auto* p = new char{};
auto address = reinterpret_cast<std::uintptr_t>(p);
assert(address % alignof(std::max_align_t) == 0);  // guaranteed
assert(address % 64 == 0);  // NOT guaranteed
```

### Strategy 4: C-style aligned allocation for heap data

```cpp
// POSIX (Linux, macOS)
#include <cstdlib>
float* data;
posix_memalign(reinterpret_cast<void**>(&data), 64, n * sizeof(float));
// Must free with free(), not delete

// C++17 (portable)
float* data = static_cast<float*>(std::aligned_alloc(64, n * sizeof(float)));
// size must be a multiple of alignment; free with std::free()

// C++17 operator new with alignment
float* data = static_cast<float*>(
    ::operator new(n * sizeof(float), std::align_val_t{64}));
// free with ::operator delete(data, std::align_val_t{64})
```

## Expected Impact

- **Split load elimination:** when split loads are frequent (every SIMD iteration), fixing alignment can yield 10-30% speedup on the affected loop.
- **perf-ninja mem_alignment_1:** the lab expects measurable improvement for matrix sizes where misalignment causes split loads (e.g., N=64, 128, 256, 512, 1024 on x86). Sizes like N=63 or N=65 may also benefit once rows are padded.
- **SIMD throughput:** aligned loads (`vmovaps`, `vld1q` with aligned address) can be faster than unaligned loads (`vmovups`) on some microarchitectures, though modern CPUs have largely closed this gap for non-split cases.
- **ARM NEON:** older ARM cores (pre-Cortex-A75) have significant penalties for unaligned NEON loads. Modern cores handle unaligned access in hardware but still penalize cache line crossings.
- **AVX-512:** 64-byte aligned data is critical for AVX-512, as a single 512-bit load that crosses a cache line boundary always triggers a split.

## Caveats

- **Modern x86 handles non-split unaligned access well:** on Intel Skylake and later, an unaligned load that stays within one cache line has zero penalty. Alignment only matters when loads *cross* cache line boundaries.
- **Padding wastes memory:** padding rows from N to the next multiple of 16 floats (64 bytes) can add up to 60 bytes per row. For tall narrow matrices, this overhead can be significant.
- **Padding changes index arithmetic:** all code accessing the matrix must use `K` (padded width) rather than `N` (logical width) for row stride. Padding columns contain garbage and must not be read as valid data.
- **Apple Silicon uses 128-byte L2 cache lines:** while L1 uses 64-byte lines, the L2 cache on M1/M2/M3 operates on 128-byte lines. For workloads that are L2-bandwidth-bound, align to 128 bytes.
- **Over-alignment for small allocations:** aligning a 4-byte int to a 64-byte boundary wastes 60 bytes. Only align data that is accessed in SIMD-width chunks or is subject to false sharing.
- **`std::aligned_alloc` size constraint:** the C++17 `std::aligned_alloc` function requires `size` to be a multiple of `alignment`. Violating this is undefined behavior. Use `posix_memalign` or `_aligned_malloc` (MSVC) if the size is not naturally a multiple of alignment.
- **Portability:** `posix_memalign` is POSIX only. On Windows, use `_aligned_malloc` / `_aligned_free`. C++17 `std::aligned_alloc` and `operator new(size, align)` are the portable alternatives.
