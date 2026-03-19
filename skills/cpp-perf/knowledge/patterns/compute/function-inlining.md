---
name: Function Inlining (qsort to std::sort)
source: perf-ninja core_bound/function_inlining_1
layers: [compiler, source]
platforms: [arm, x86]
keywords: [inlining, qsort, std::sort, comparator, function call overhead, prologue, epilogue]
---

## Problem

Using C-style `qsort` with a function pointer comparator prevents the compiler from inlining the comparison function. Each element comparison incurs a full indirect function call (prologue, epilogue, indirect branch). For sorting N=10000 elements this means O(N*log(N)) ~130K non-inlineable calls.

`std::sort` with a lambda or functor allows the compiler to inline the comparator, eliminating call overhead and enabling further optimizations (e.g., vectorization of comparison chains).

## Detection

- Profile shows hot function prologue/epilogue in a comparator function
- Code uses `qsort()` with a C function pointer comparator
- Code uses `std::sort` with a `std::function` (which also blocks inlining)
- Any hot tight loop calling a function through a pointer

## Transformation

**Before** (init.cpp baseline -- qsort with function pointer):
```cpp
static int compare(const void *lhs, const void *rhs) {
  auto &a = *reinterpret_cast<const S *>(lhs);
  auto &b = *reinterpret_cast<const S *>(rhs);

  if (a.key1 < b.key1) return -1;
  if (a.key1 > b.key1) return 1;
  if (a.key2 < b.key2) return -1;
  if (a.key2 > b.key2) return 1;
  return 0;
}

void solution(std::array<S, N> &arr) {
  qsort(arr.data(), arr.size(), sizeof(S), compare);
}
```

**After** (use std::sort with inlineable lambda/operator<):
```cpp
void solution(std::array<S, N> &arr) {
  std::sort(arr.begin(), arr.end(), [](const S &a, const S &b) {
    if (a.key1 != b.key1)
      return a.key1 < b.key1;
    return a.key2 < b.key2;
  });
}
```

## Expected Impact

- 2-5x speedup on typical sort workloads
- Eliminates ~130K indirect function calls for N=10000
- Enables further compiler optimizations (SIMD comparison, branch optimization)

## Caveats

- Only beneficial when the comparator is small and called frequently
- If comparator is complex (hundreds of lines), inlining may cause code bloat and hurt i-cache
- `std::sort` is not a stable sort; use `std::stable_sort` if order preservation of equal elements matters
- If comparator must be selected at runtime, consider devirtualization or template-based dispatch instead
