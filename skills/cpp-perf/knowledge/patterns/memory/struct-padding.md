---
name: Struct Member Ordering and Padding Optimization
source: Cpp-High-Performance Ch.7, C++ standard [class.mem]
layers: [compiler]
platforms: [arm, x86]
keywords: [struct, padding, alignment, member order, sizeof, pack, no_unique_address, cache line]
---

## Problem

C++ structs insert implicit **padding bytes** between members to satisfy alignment requirements. The compiler aligns each member to a boundary equal to its size (or its `alignof` value), and the overall struct size is padded to a multiple of the largest member's alignment. Poor member ordering wastes memory and reduces cache efficiency.

**Example -- worst case ordering:**
```cpp
struct BadLayout {
    bool   a;     // offset 0, size 1
    // 7 bytes padding (double requires 8-byte alignment)
    double b;     // offset 8, size 8
    int    c;     // offset 16, size 4
    // 4 bytes padding (struct alignment = 8, need multiple of 8)
};
// sizeof(BadLayout) = 24 bytes, but only 13 bytes of actual data
```

**Same members, optimal ordering:**
```cpp
struct GoodLayout {
    double b;     // offset 0, size 8
    int    c;     // offset 8, size 4
    bool   a;     // offset 12, size 1
    // 3 bytes padding (struct alignment = 8)
};
// sizeof(GoodLayout) = 16 bytes -- 33% smaller
```

The wasted space compounds across arrays. An array of 1 million `BadLayout` objects wastes 8 MB compared to `GoodLayout`. Larger structs also mean fewer fit in a cache line, increasing cache misses for iteration-heavy workloads.

## Detection

**Source-level indicators:**
- Struct members not ordered by size
- Small members (bool, char, int8_t) placed before large members (double, int64_t, pointers)
- Structs with unexpectedly large `sizeof` relative to the sum of member sizes
- Hot structs stored in large arrays or containers

**Compile-time detection:**
```cpp
// Static assert to catch unexpected padding
struct MyStruct { /* ... */ };
static_assert(sizeof(MyStruct) == expected_size, "Unexpected padding in MyStruct");

// GCC/Clang: warn about padding
// Compile with: -Wpadded
// warning: padding struct 'BadLayout' with 7 bytes to align 'b'
```

**Tooling:**
```bash
# Clang: dump record layouts
clang++ -cc1 -fdump-record-layouts myfile.cpp 2>&1 | grep -A 20 'MyStruct'

# pahole (from dwarves package): shows padding in compiled binary
pahole -C MyStruct ./myapp
```

**Runtime detection:**
```cpp
#include <cstddef>
// Check if struct has padding
template<typename T>
constexpr bool has_padding() {
    // Sum of member sizes < sizeof(T) means padding exists
    return !std::has_unique_object_representations_v<T>;
}
```

## Transformation

### Strategy 1: Sort members by size descending

Place the largest members first, then progressively smaller ones:

```cpp
// Before: random ordering, sizeof = 32
struct Particle {
    bool   active;      // 1 byte + 7 padding
    double x;           // 8 bytes
    int    id;          // 4 bytes + 4 padding
    double y;           // 8 bytes
};

// After: sorted by size descending, sizeof = 24
struct Particle {
    double x;           // 8 bytes
    double y;           // 8 bytes
    int    id;          // 4 bytes
    bool   active;      // 1 byte + 3 padding
};
```

### Strategy 2: Group same-sized members together

When sorting by size is not sufficient (e.g., many mixed-size members), group by alignment:

```cpp
struct OptimalLayout {
    // 8-byte aligned members
    double position_x;
    double position_y;
    void*  next_ptr;

    // 4-byte aligned members
    int    id;
    float  velocity;
    uint32_t flags;

    // 2-byte aligned members
    int16_t  temperature;
    uint16_t generation;

    // 1-byte aligned members
    bool   active;
    uint8_t type;
    char   padding_explicit[2];  // make padding intentional
};
```

### Strategy 3: Use `[[no_unique_address]]` for empty members (C++20)

Empty classes and tag types still occupy at least 1 byte by default. `[[no_unique_address]]` allows them to share space:

```cpp
struct Allocator {};  // empty class, sizeof = 1

// Before: empty allocator wastes 8 bytes (with padding)
struct Container {
    double* data;           // 8 bytes
    size_t  size;           // 8 bytes
    Allocator alloc;        // 1 byte + 7 padding
};  // sizeof = 32

// After: empty allocator overlaps with another member
struct Container {
    double* data;                              // 8 bytes
    size_t  size;                              // 8 bytes
    [[no_unique_address]] Allocator alloc;     // 0 bytes
};  // sizeof = 16
```

This is particularly valuable for policy-based designs and allocator-aware containers.

### Strategy 4: Use `#pragma pack` judiciously

Force tight packing when memory savings outweigh alignment penalties:

```cpp
// WARNING: misaligned access may be slower or cause faults on some ARM cores
#pragma pack(push, 1)
struct PackedHeader {
    uint8_t  type;       // 1 byte
    uint32_t length;     // 4 bytes (misaligned!)
    uint16_t checksum;   // 2 bytes
};  // sizeof = 7 instead of 12
#pragma pack(pop)
```

Use packed structs only for:
- Wire protocols / file formats where layout must match a specification
- Memory-mapped I/O registers
- Situations where memory savings are critical and access is infrequent

### Strategy 5: Bit fields for flag-heavy structs

```cpp
// Before: each bool is 1 byte + potential padding
struct Flags {
    bool visible;     // 1 byte
    bool enabled;     // 1 byte
    bool selected;    // 1 byte
    bool dirty;       // 1 byte
    double value;     // 8 bytes
};  // sizeof = 16

// After: bit fields pack flags into minimal space
struct Flags {
    double value;     // 8 bytes
    bool visible  : 1;
    bool enabled  : 1;
    bool selected : 1;
    bool dirty    : 1;
};  // sizeof = 16 (same here, but wins with more flags)
```

## Expected Impact

- **Memory reduction:** typically 10-40% for poorly ordered structs. The `{bool, double, int}` vs `{double, int, bool}` example saves 33%.
- **Cache efficiency:** smaller structs = more objects per cache line = fewer cache misses when iterating over arrays. For a struct reduced from 48 to 40 bytes, cache miss rate drops because 1.6 objects per cache line vs 1.3.
- **Bandwidth:** reducing struct size proportionally reduces memory bandwidth consumption for sequential scans.
- **No algorithmic change:** this is a pure layout optimization -- only the member declaration order changes, no functional code modification needed.

## Caveats

- **`#pragma pack` performance penalty:** misaligned access on x86 costs 2-3x for crossing cache line boundaries. On some ARM cores (Cortex-A53), misaligned access to device memory causes a fault. Use packed structs only for serialization, never for hot data structures.
- **ABI compatibility:** changing member order changes the binary layout. This breaks serialization, memory-mapped file formats, and any code that depends on offsetof() values. Coordinate changes across all consumers.
- **False sharing consideration:** for multi-threaded access, members written by different threads should be on different cache lines. Padding may be intentionally added (not removed) via `alignas(64)` to prevent false sharing -- this conflicts with the "minimize padding" goal.
- **Compiler may reorder in some cases:** C++ guarantees member order within an access specifier section. Members across different access specifiers (`public:` / `private:`) may be reordered by the compiler (though most compilers do not do this).
- **Readability trade-off:** grouping by size rather than by logical purpose can hurt code readability. Use comments to explain the ordering rationale.
- **`[[no_unique_address]]` support:** requires C++20. Older standard modes need alternative approaches (compressed pair, EBO base class).
- **Bit fields are not atomic:** bit field access involves read-modify-write of the containing storage unit. Never use bit fields for concurrently accessed flags -- use `std::atomic` instead.
