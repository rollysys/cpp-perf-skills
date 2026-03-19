---
name: Array of Structures to Structure of Arrays
source: perf-ninja memory_bound/data_packing, perf-book Ch.8
layers: [microarchitecture, system]
platforms: [arm, x86]
keywords: [AoS, SoA, struct, cache line, spatial locality, data packing, hot fields, cold fields]
---

## Problem

When iterating over an array of structures but only accessing a subset of fields, the AoS layout forces entire structures into cache lines. Fields that are never touched ("cold fields") waste cache bandwidth and evict useful data. This is especially costly when the structure is large relative to the cache line size (typically 64 bytes).

Common trigger: a loop that reads or writes only 1-2 fields out of a struct with 5+ fields.

```cpp
// AoS layout -- struct S is 40 bytes (with padding)
struct S {
  int i;          // hot: used for sorting
  long long l;    // cold
  short s;        // cold
  double d;       // cold
  bool b;         // cold
};
std::vector<S> arr(N);

// Only field 'i' is accessed, but all 40 bytes per element are fetched
for (const auto& v : arr) {
  ++cnt[v.i - minRandom + 1];
}
```

## Detection

**Source-level indicators:**
- Loops that access only a subset of struct fields
- Large structs (> 32 bytes) iterated in hot loops
- `sizeof(S)` much larger than the sum of accessed field sizes

**Profile-level indicators:**
- High L1/L2/L3 cache miss rate on load instructions within the loop
- Memory bandwidth utilization disproportionate to useful data processed
- TMA: high `Memory_Bound > L1_Bound` or `L2_Bound` metrics

**Disassembly clues:**
- Load instructions with large stride offsets between iterations (matching `sizeof(S)`)

## Transformation

### Strategy 1: Structure Splitting (hot/cold separation)

Split the struct into hot and cold parts, store in parallel arrays:

```cpp
// Before: AoS
struct S {
  int i;
  long long l;
  short s;
  double d;
  bool b;
};
std::vector<S> arr(N);

for (const auto& v : arr) {
  ++cnt[v.i - minRandom + 1];
}
```

```cpp
// After: Split into hot and cold structs
struct S_Hot {
  int i;
};
struct S_Cold {
  long long l;
  short s;
  double d;
  bool b;
};
std::vector<S_Hot> arr_hot(N);
std::vector<S_Cold> arr_cold(N);

for (const auto& v : arr_hot) {
  ++cnt[v.i - minRandom + 1];
}
```

Now each cache line holds 16 `int` values instead of ~1.5 full structs. The counting loop touches only 4 bytes per element instead of 40.

### Strategy 2: Full SoA transformation

For maximum flexibility, decompose all fields into separate arrays:

```cpp
// After: full SoA
struct DataSoA {
  std::vector<int> i;
  std::vector<long long> l;
  std::vector<short> s;
  std::vector<double> d;
  std::vector<bool> b;
};
DataSoA data;
// Each phase accesses only the arrays it needs
```

### Strategy 3: Data Packing (reduce struct size)

From perf-ninja data_packing: reduce the struct size by eliminating padding and using smaller types:

```cpp
// Before: 40 bytes with padding
struct S {
  int i;          // 4 bytes
  long long l;    // 8 bytes (+ 4 padding before)
  short s;        // 2 bytes
  double d;       // 8 bytes (+ 6 padding before)
  bool b;         // 1 byte (+ 7 padding after)
};

// After: reorder by size descending, use smaller types, bitfields
struct S {
  double d;       // 8 bytes
  long long l;    // 8 bytes
  int i;          // 4 bytes
  short s;        // 2 bytes
  bool b;         // 1 byte (+1 padding)
};  // 24 bytes -- 40% reduction
```

## Expected Impact

- **Cache utilization improvement:** If only 1 field out of 5 is accessed, SoA or splitting can improve effective cache utilization by 5-10x for that loop.
- **Typical speedup:** 2-5x for memory-bound loops that touch a small fraction of fields.
- **perf-ninja data_packing lab:** reports measurable improvement from reducing struct size from 40 to 24 bytes (or smaller with bitfields).
- **Cache lines per iteration:** AoS with 40-byte struct: ~0.6 cache lines/element. SoA with 4-byte field: ~0.06 cache lines/element.

## Caveats

- **Do NOT apply when all fields are accessed together** in the hot loop -- SoA would scatter related data across memory and hurt performance.
- **SoA complicates code maintenance:** adding a new field requires updating multiple arrays. Consider using a struct-of-arrays library or code generation.
- **Bitfield packing adds computation overhead:** extracting/inserting bitfields requires shift and mask operations. Only beneficial when memory transfer cost exceeds the extra ALU work.
- **Thread safety:** with AoS, each element is independent. With SoA, concurrent writes to different fields of the same logical element touch different cache lines, which is usually fine but changes the access pattern.
- **Bitfield portability:** bitfield layout is implementation-specific. MSVC may refuse to pack bitfields of different types into the same storage unit.
- **Alignment requirements:** some SIMD operations require aligned data. SoA arrays of primitive types naturally align well; packed structs may not.
