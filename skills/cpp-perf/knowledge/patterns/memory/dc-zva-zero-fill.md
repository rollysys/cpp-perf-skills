---
name: DC ZVA Cache-Line Zero Fill (Bypass Read-for-Ownership)
source: optimized-routines string/aarch64/memset.S
layers: [system, microarchitecture]
platforms: [arm]
keywords: [DC ZVA, cache zero, memset, zero fill, write allocate, cache line, read for ownership]
---

## Problem

When writing zeros to a large buffer (memset to zero, calloc, zero-initialization), a normal store (STP) triggers a **read-for-ownership (RFO)** transaction: the CPU must first read the cache line from memory (or lower cache) into L1, then overwrite it with zeros. This read is entirely wasted -- we are about to overwrite every byte anyway.

ARM's `DC ZVA` (Data Cache Zero by Virtual Address) instruction zeros an entire cache line (typically 64 bytes) WITHOUT the RFO read. The cache line is allocated directly in a zeroed state, cutting memory bandwidth in half for large zero-fills.

ARM's optimized-routines `memset.S` uses `DC ZVA` for the zero-fill fast path and falls back to STP pairs for non-zero values.

## Detection

- `memset(ptr, 0, large_size)` in hot paths
- `calloc` or `new T()` for large allocations (OS may already use similar techniques)
- Zero-initialization of large arrays/matrices: `std::vector<int> v(N, 0)`, `memset(matrix, 0, sizeof(matrix))`
- Profile shows high memory bandwidth utilization for write-dominated workloads
- PMU counter `BUS_ACCESS_WR` or equivalent showing writes to memory for code that only zeroes buffers

## Transformation

### DC ZVA from optimized-routines memset.S (annotated)

```
// ARM assembly from optimized-routines memset.S (simplified)
// x0 = dst, x1 = value (0 for zero-fill), x2 = count

// Step 1: Check if value is zero. DC ZVA only works for zero-fill.
    cbnz  w1, .Lnon_zero_fill

// Step 2: Query the ZVA block size (usually 64 bytes, but architecturally variable)
    mrs   x3, dczid_el0           // read DC ZVA block size register
    tbnz  w3, #4, .Lno_zva        // bit 4 = ZVA disabled on this system
    and   w3, w3, #0xf            // bits[3:0] = log2(block_size / 4)
    mov   w4, #4
    lsl   w3, w4, w3              // block_size = 4 << dczid_el0[3:0]
    // w3 = ZVA block size (usually 64)

// Step 3: Align destination to ZVA block boundary using STP
    neg   x4, x0
    and   x4, x4, x3              // bytes until aligned = (-dst) & (block_size - 1)
    sub   x2, x2, x4              // remaining count after alignment

    // Zero the alignment prefix with STP (store pair of zero registers)
.Lalign_loop:
    stp   xzr, xzr, [x0], #16    // store 16 zero bytes
    subs  x4, x4, #16
    b.gt  .Lalign_loop

// Step 4: Zero full cache lines with DC ZVA
    sub   x5, x2, x3              // stop before last partial block
.Lzva_loop:
    dc    zva, x0                 // zero entire cache line (64B), NO read-for-ownership
    add   x0, x0, x3              // advance by block size
    subs  x5, x5, x3
    b.ge  .Lzva_loop

// Step 5: Zero the tail with STP
.Ltail:
    stp   xzr, xzr, [x0], #16
    subs  x2, x2, #16
    b.gt  .Ltail
    ret
```

### C equivalent using inline assembly

```cpp
#include <cstddef>
#include <cstdint>

// Query the ZVA block size at runtime (usually 64 bytes)
inline size_t get_zva_block_size() {
    uint64_t dczid;
    asm volatile("mrs %0, dczid_el0" : "=r"(dczid));
    if (dczid & (1 << 4)) return 0;  // ZVA disabled
    return 4u << (dczid & 0xf);
}

// Zero-fill using DC ZVA for the bulk, STP for alignment and tail
void fast_zero_fill(void* dst, size_t n) {
    uint8_t* p = static_cast<uint8_t*>(dst);
    size_t block_size = get_zva_block_size();

    if (block_size == 0 || n < block_size * 2) {
        // ZVA not available or buffer too small -- use regular memset
        __builtin_memset(dst, 0, n);
        return;
    }

    // Phase 1: Align to block boundary using regular stores
    size_t align_bytes = (-reinterpret_cast<uintptr_t>(p)) & (block_size - 1);
    if (align_bytes > 0) {
        __builtin_memset(p, 0, align_bytes);
        p += align_bytes;
        n -= align_bytes;
    }

    // Phase 2: DC ZVA for full cache lines
    size_t blocks = n / block_size;
    for (size_t i = 0; i < blocks; ++i) {
        asm volatile("dc zva, %0" : : "r"(p) : "memory");
        p += block_size;
    }
    n -= blocks * block_size;

    // Phase 3: Tail with regular stores
    if (n > 0) {
        __builtin_memset(p, 0, n);
    }
}
```

### Why DC ZVA is faster than STP

```
Normal store (STP xzr, xzr, [x0]):
  1. CPU checks L1 cache for the target cache line
  2. Cache MISS -> send Read-For-Ownership (RFO) to L2/L3/memory
  3. Wait for the ENTIRE 64-byte cache line to arrive (latency: 4-100+ cycles)
  4. Overwrite it with zeros
  5. Mark as Modified in cache coherence protocol

DC ZVA:
  1. Allocate a cache line in L1, fill it with zeros (NO read from memory)
  2. Mark as Modified
  3. Done. Zero latency for the data fill.
```

For a 1MB zero-fill, normal STP reads 1MB of useless data from memory, then writes 1MB of zeros. DC ZVA writes 1MB of zeros with zero reads. This effectively doubles the available memory bandwidth.

## Expected Impact

| Buffer size | STP memset | DC ZVA memset | Speedup |
|------------|-----------|---------------|---------|
| 4 KB | ~200 ns | ~120 ns | 1.6x |
| 64 KB (L1 miss, L2 hit) | ~5 us | ~3 us | 1.7x |
| 1 MB (L2 miss) | ~150 us | ~80 us | 1.9x |
| 16 MB (memory bound) | ~3 ms | ~1.5 ms | 2.0x |

The speedup approaches 2x for memory-bandwidth-bound workloads because the RFO read traffic is completely eliminated.

## Caveats

- **DC ZVA only works for zero-fill.** For `memset(ptr, 0xFF, n)` or any non-zero value, DC ZVA cannot be used. ARM's memset.S falls back to `dup v0.16b, w1` + `stp q0, q0` for non-zero fills.
- **ZVA block size is implementation-defined.** Most ARM cores use 64 bytes, but some use 32 or 128. Always query `dczid_el0` at runtime. Cache the result -- it does not change during execution.
- **ZVA can be disabled by the OS/hypervisor.** Bit 4 of `dczid_el0` indicates ZVA is prohibited. Always check before use.
- **Destination must be aligned to the ZVA block size.** Unaligned DC ZVA causes an alignment fault on most implementations. The alignment prologue is mandatory.
- **DC ZVA in a VM/container.** Some hypervisors trap DC ZVA, making it slower than normal stores. Profile on your actual deployment platform.
- **Not useful for small buffers.** The alignment overhead makes DC ZVA slower than STP for buffers smaller than ~2x the block size. ARM's memset uses STP for small sizes.
- **Interaction with cache coherence:** DC ZVA allocates the line in Modified state. If another core holds the line in Shared state, a coherence invalidation is still required. The benefit is avoiding the data transfer, not the coherence traffic.
- **glibc/musl already use this.** On ARM Linux, the system `memset` and `calloc` typically already use DC ZVA. This pattern is most useful when you have a custom allocator or are writing bare-metal/kernel code.
