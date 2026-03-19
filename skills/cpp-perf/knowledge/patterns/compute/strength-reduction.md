---
name: Strength Reduction
source: Cpp-High-Performance Ch.3-4
layers: [algorithm, language]
platforms: [arm, x86]
keywords: [strength reduction, division, multiplication, shift, modulo, power of two, lookup, precompute]
---

## Problem

Programs frequently use operations that are far more expensive than necessary for the task at hand. Strength reduction replaces expensive operations with cheaper equivalents that produce the same result. The cost hierarchy on modern CPUs (approximate latency in cycles):

| Operation | ARM (Cortex-A76) | x86 (Intel Alder Lake) |
|-----------|-------------------|------------------------|
| Bitwise AND/OR/XOR/shift | 1 cycle | 1 cycle |
| Integer add/sub | 1 cycle | 1 cycle |
| Integer multiply | 3 cycles | 3 cycles |
| Integer divide (32-bit) | 7-12 cycles | 20-90+ cycles |
| FP add | 3-4 cycles | 3-4 cycles |
| FP multiply | 3-4 cycles | 4-5 cycles |
| FP divide | 7-13 cycles | 11-21 cycles |
| Branch mispredict | ~12 cycles | ~14 cycles |
| L1 cache hit | 4 cycles | 4-5 cycles |
| L2 cache hit | ~10 cycles | ~12 cycles |
| LLC cache hit | ~30 cycles | ~40 cycles |
| DRAM access | ~100 cycles | ~60-100 cycles |

The biggest wins come from replacing divisions and modulo operations, upgrading algorithmic complexity, and precomputing results to trade space for time.

## Detection

**Source-level indicators:**
- Division or modulo by a constant: `x / 10`, `x % 16`
- Division or modulo by a power of 2 that is not using shift/mask: `x / 4` on unsigned types (compiler usually handles this, but signed division is trickier)
- Repeated computation of the same value across iterations
- Linear search over sorted data
- Repeated traversal of data to compute aggregates
- Multiplication where one operand is a power of 2
- Floating-point division where the divisor is loop-invariant (can be replaced with multiply-by-reciprocal)

**Assembly-level indicators:**
- ARM: `udiv`/`sdiv` instructions in hot loops (these are multi-cycle)
- x86: `div`/`idiv` instructions in hot loops (these are extremely expensive, 20-90+ cycles, and cannot be pipelined)
- Repeated `ldr`/`mov` sequences that load the same value

**Profiling indicators:**
- High `Core Bound` percentage with low IPC
- Long latency stalls on division micro-ops

## Transformation

### Pattern 1: Division/modulo by power of 2 to bitwise operations

**Before:**
```cpp
// Division and modulo by powers of 2
unsigned align_to_cacheline(unsigned addr) {
    unsigned offset = addr % 64;           // expensive: hardware division
    unsigned aligned = addr - offset;
    unsigned next_line = aligned + 64;
    unsigned slot = addr / 64;             // expensive: hardware division
    return slot;
}
```

**After:**
```cpp
unsigned align_to_cacheline(unsigned addr) {
    unsigned offset = addr & 63;           // 1 cycle: bitwise AND
    unsigned aligned = addr - offset;      // equivalent: addr & ~63
    unsigned next_line = aligned + 64;
    unsigned slot = addr >> 6;             // 1 cycle: shift right
    return slot;
}
```

General rules for **unsigned** integers:
- `x % (2^n)` --> `x & ((1 << n) - 1)` -- bitwise AND with mask
- `x / (2^n)` --> `x >> n` -- right shift
- `x * (2^n)` --> `x << n` -- left shift

Note: compilers generally do this for unsigned types. For **signed** types, extra correction is needed (the shift rounds toward negative infinity, while division rounds toward zero), so the compiler inserts additional instructions. Making the variable `unsigned` when values are non-negative lets the compiler emit a simple shift.

### Pattern 2: Constant division to multiply-by-reciprocal

For division by a non-power-of-2 constant, compilers replace division with a multiply+shift sequence (Barrett reduction). But for floating-point with a loop-invariant divisor, you should hoist the reciprocal:

**Before:**
```cpp
void normalize(float* data, int n, float range) {
    for (int i = 0; i < n; i++) {
        data[i] = data[i] / range;  // FP divide: ~10-20 cycles
    }
}
```

**After:**
```cpp
void normalize(float* data, int n, float range) {
    float inv_range = 1.0f / range;  // one division
    for (int i = 0; i < n; i++) {
        data[i] = data[i] * inv_range;  // FP multiply: ~4 cycles
    }
}
```

FP divide throughput is typically 1 per 4-7 cycles (vs multiply at 1 per 0.5 cycles), so this can give 8-14x throughput improvement for the division-heavy loop. Note: multiply-by-reciprocal may introduce a small rounding difference, requiring `-ffast-math` or explicit opt-in.

### Pattern 3: Algorithmic complexity reduction (linear search to binary search)

From Cpp-High-Performance Chapter 3 -- replacing O(n) search with O(log n):

**Before** -- linear search, O(n):
```cpp
auto linear_search(const std::vector<int>& a, int key) {
    for (const auto& value : a) {
        if (value == key) {
            return true;
        }
    }
    return false;
}
```

**After** -- binary search on sorted data, O(log n):
```cpp
auto binary_search(const std::vector<int>& a, int key) {
    if (a.empty()) return false;
    auto low = size_t(0);
    auto high = a.size() - 1;
    while (low <= high) {
        const auto mid = low + ((high - low) / 2);
        if (a[mid] < key)       low = mid + 1;
        else if (a[mid] > key)  high = mid - 1;
        else                    return true;
    }
    return false;
}
```

For n = 1,000,000 elements: linear search averages 500,000 comparisons; binary search needs at most 20. This is a **25,000x** reduction in comparisons. In practice, the speedup is moderated by branch mispredictions in binary search and the fact that linear search is cache-friendly, but for large n binary search wins overwhelmingly.

The key prerequisite: data must be sorted. If searches are frequent and data changes rarely, paying the O(n log n) sort cost once is amortized over many O(log n) lookups.

### Pattern 4: Data layout optimization (reducing object size for cache efficiency)

From Cpp-High-Performance Chapter 4 (sum_scores.cpp) -- reducing data structure size improves iteration speed by increasing cache density:

**Before** -- large objects waste cache lines:
```cpp
struct BigObject {
    std::array<char, 256> data_{};   // 256 bytes of rarely-used payload
    int score_{};                     // 4 bytes of frequently-accessed data
};
// sizeof(BigObject) = 260 bytes
// 1M objects = 260 MB, ~4 objects per cache line used

auto sum_scores(const std::vector<BigObject>& objects) {
    auto sum = 0;
    for (const auto& obj : objects)
        sum += obj.score_;   // touches 4 bytes but loads 64 bytes (cache line)
    return sum;
}
```

**After** -- separate hot and cold fields:
```cpp
struct SmallObject {
    std::array<char, 4> data_{};
    int score_{};
};
// sizeof(SmallObject) = 8 bytes
// 1M objects = 8 MB, ~8 objects per cache line

// Or even better: split into parallel arrays (SoA)
struct ScoreData {
    std::vector<int> scores;           // hot path: dense, cache-friendly
    std::vector<std::array<char, 256>> payloads; // cold path: separate
};
```

This is "strength reduction" at the data structure level: reducing the cost of each memory access by ensuring more useful data per cache line. The cache_thrashing.cpp example shows that row-major vs column-major traversal matters enormously when data exceeds L1 cache size.

### Pattern 5: Precomputation (trade space for time)

**Before** -- recomputing expensive values:
```cpp
unsigned getSumOfDigits(unsigned n) {
    unsigned sum = 0;
    while (n != 0) {
        sum += n % 10;  // two expensive ops per digit: divide + modulo
        n /= 10;
    }
    return sum;
}
```

**After** -- lookup table for partial results:
```cpp
// Precompute sum-of-digits for 0..9999
static std::array<uint8_t, 10000> digit_sum_table = [] {
    std::array<uint8_t, 10000> t{};
    for (int i = 0; i < 10000; i++) {
        int s = 0, v = i;
        while (v) { s += v % 10; v /= 10; }
        t[i] = s;
    }
    return t;
}();

unsigned getSumOfDigitsFast(unsigned n) {
    unsigned sum = 0;
    // Process 4 digits at a time using table lookup
    while (n >= 10000) {
        sum += digit_sum_table[n % 10000];
        n /= 10000;
    }
    sum += digit_sum_table[n];
    return sum;
}
```

The 10,000-entry table fits in ~10 KB (well within L1 cache). Reduces the number of divisions by ~4x and replaces per-digit computation with a single table lookup per group.

## Expected Impact

| Transformation | Typical Speedup | Conditions |
|---|---|---|
| Power-of-2 div/mod to shift/mask | 5-20x per operation | Unsigned types; compiler may already do this |
| FP divide to multiply-by-reciprocal | 4-14x throughput | Loop-invariant divisor; accept rounding difference |
| Linear to binary search | 10-25,000x (depends on n) | Data is sorted or can be sorted once |
| Data layout (big to small objects) | 2-10x for traversal | Bottleneck is memory bandwidth |
| Precomputation with lookup table | 2-5x | Table fits in L1/L2 cache |
| Row-major vs column-major traversal | 3-10x | Matrix exceeds L1 cache size |

## Caveats

- **Compiler already does it:** Modern compilers (GCC, Clang at -O2 and above) automatically convert unsigned division/modulo by compile-time constants to shift/multiply sequences. Check the assembly before manually optimizing -- you may be doing redundant work.
- **Signed vs unsigned:** Signed division by powers of 2 requires extra instructions for correct rounding toward zero. Making variables `unsigned` when values are non-negative enables simpler codegen.
- **Multiply-by-reciprocal accuracy:** For floating-point, `x * (1.0/d)` is not bit-exact equivalent to `x / d`. This matters for numerical algorithms that depend on exact rounding. Requires `-ffast-math` or manual opt-in.
- **Lookup table cache pollution:** Large lookup tables (> L1 cache) can evict hot data and hurt overall performance. The table must be frequently reused to stay in cache. For cold-path usage, recomputation may be faster.
- **Sorting prerequisite for binary search:** If the data is modified frequently, maintaining sorted order (or using a balanced BST / hash map) has ongoing cost. Binary search only wins if the amortized sort cost is less than the saved search cost.
- **Branch mispredictions in binary search:** Binary search has unpredictable branches (each comparison is ~50/50). For small arrays (n < 64), a branchless linear scan may outperform binary search because it avoids mispredictions and is SIMD-friendly. Consider `std::lower_bound` with branchless comparison or SIMD-accelerated search for small n.
- **Data layout changes are invasive:** Splitting structures into SoA (Structure of Arrays) requires significant refactoring and makes code harder to maintain. Only apply when profiling confirms memory bandwidth is the bottleneck.
