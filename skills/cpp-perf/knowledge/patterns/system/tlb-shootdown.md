---
name: TLB Shootdown Avoidance
source: perf-book Ch.12, Linux kernel documentation, Intel SDM Vol.3 Ch.4
layers: [system]
platforms: [arm, x86]
keywords: [TLB shootdown, IPI, munmap, mprotect, madvise, NUMA balancing, transparent huge pages, interrupt]
---

## Problem

In a multithreaded application running on multiple cores, any operation that modifies virtual-to-physical address mappings must invalidate the corresponding TLB entries on **all cores** that may have cached them. This is done via **TLB shootdown**: the initiating core sends Inter-Processor Interrupts (IPIs) to all other cores, each of which must stop execution, flush the relevant TLB entries, and acknowledge. This entire process can cost **5-50 microseconds**, stalling all affected cores.

Syscalls that trigger TLB shootdowns:
- `munmap()` -- unmapping memory
- `mprotect()` -- changing page permissions
- `madvise(MADV_DONTNEED/FREE)` -- releasing pages
- `mremap()` -- remapping memory regions
- `brk()` / `sbrk()` -- shrinking the data segment
- Transparent Huge Page promotion/demotion by `khugepaged`
- NUMA balancing page migrations

The impact is proportional to the number of cores: on a 64-core machine, a single `munmap()` in any thread can stall all 63 other cores for 10-50us each.

```cpp
// Problematic: frequent munmap on a hot path in a 32-thread application
void process_batch(char* buf, size_t size) {
    // ... process data ...
    munmap(buf, size);  // triggers IPI to all 31 other cores
    // Each core: interrupt -> TLB flush -> acknowledge -> resume
    // Total system impact: 31 cores * 10us = 310 core-microseconds wasted
}
```

## Detection

**Direct measurement (Linux):**
```bash
# Watch TLB shootdown IPIs in real-time
watch -d 'grep TLB /proc/interrupts'
# The TLB column shows cumulative IPI count per CPU

# Sample: before and after running the application
grep TLB /proc/interrupts > /tmp/before
./myapp
grep TLB /proc/interrupts > /tmp/after
diff /tmp/before /tmp/after
```

**Profile-level indicators:**
```bash
# Trace mmap/munmap/mprotect syscalls
perf trace -e mmap,munmap,mprotect,madvise -p $(pidof myapp) 2>&1 | head -100

# Count memory-management syscalls
strace -c -e trace=memory ./myapp
# High munmap/mprotect counts indicate shootdown risk

# Measure IPI overhead directly
perf stat -e irq_vectors:call_function_single_entry ./myapp
```

**Source-level indicators:**
- `free()` of large allocations (glibc uses `munmap` for allocations > 128KB)
- Explicit `munmap`, `mprotect`, `madvise(MADV_DONTNEED)` on hot paths
- Memory allocators that aggressively return memory to the OS
- Use of `std::vector::shrink_to_fit()` or reallocation patterns that free old buffers

**Characteristic symptom:** multi-threaded application where adding more threads causes disproportionate slowdown, with unexpected idle time visible in profilers. Latency spikes correlate across all threads simultaneously.

## Transformation

### Strategy 1: Avoid munmap/mprotect on hot paths

Reuse memory instead of returning it to the OS:

```cpp
// Before: allocate and free per batch -- triggers munmap + shootdown
void process_batch() {
    void* buf = mmap(nullptr, BATCH_SIZE, PROT_READ | PROT_WRITE,
                     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    process(buf, BATCH_SIZE);
    munmap(buf, BATCH_SIZE);  // TLB shootdown!
}

// After: reuse a persistent buffer pool
class BufferPool {
    std::vector<void*> free_list;
    std::mutex mtx;
public:
    void* acquire(size_t size) {
        std::lock_guard lk(mtx);
        if (!free_list.empty()) {
            void* p = free_list.back();
            free_list.pop_back();
            return p;
        }
        return mmap(nullptr, size, PROT_READ | PROT_WRITE,
                    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    }
    void release(void* p) {
        std::lock_guard lk(mtx);
        free_list.push_back(p);  // no munmap, no shootdown
    }
};
```

### Strategy 2: Prevent glibc from calling munmap

```cpp
#include <malloc.h>

// Prevent free() from triggering munmap
mallopt(M_MMAP_MAX, 0);         // never use mmap for allocations
mallopt(M_TRIM_THRESHOLD, -1);  // never trim the heap

// Or use jemalloc/tcmalloc which are less aggressive about returning memory:
// jemalloc: MALLOC_CONF="retain:true,muzzy_decay_ms:-1,dirty_decay_ms:-1"
// tcmalloc: MallocExtension::instance()->SetNumericProperty(
//     "tcmalloc.aggressive_memory_decommit", 0);
```

### Strategy 3: Disable NUMA balancing

Linux NUMA balancing migrates pages between NUMA nodes, causing shootdowns:

```bash
# Disable NUMA balancing system-wide
echo 0 > /proc/sys/kernel/numa_balancing

# Or per-process via numactl
numactl --membind=0 --cpunodebind=0 ./myapp
```

### Strategy 4: Disable Transparent Huge Pages

THP's `khugepaged` daemon merges/splits pages, triggering shootdowns:

```bash
# Disable THP entirely
echo never > /sys/kernel/mm/transparent_hugepage/enabled

# Or disable just the khugepaged daemon (keeps existing THPs)
echo 0 > /sys/kernel/mm/transparent_hugepage/khugepaged/defrag
```

If huge pages are needed for TLB reach, use explicit huge pages (`MAP_HUGETLB`) which do not trigger background promotion/demotion.

### Strategy 5: Use madvise(MADV_FREE) instead of munmap

`MADV_FREE` (Linux 4.5+) marks pages as reclaimable but does not immediately unmap them, avoiding an immediate shootdown:

```cpp
// Instead of munmap (immediate shootdown):
// munmap(buf, size);

// Use MADV_FREE (lazy reclamation, deferred shootdown):
madvise(buf, size, MADV_FREE);
// Pages remain mapped but kernel can reclaim them under memory pressure
// Re-accessing the pages is free if they haven't been reclaimed
```

Note: `MADV_FREE` may still cause a deferred shootdown when the kernel reclaims pages. For zero-shootdown operation, keep buffers allocated and never call munmap or madvise.

## Expected Impact

- **Per-shootdown cost:** 5-50us stall on each affected core. On a 32-core machine, each shootdown wastes up to 1.6 milliseconds of aggregate CPU time.
- **Eliminating shootdowns on hot paths:** reduces p99 latency by 10-100us in multi-threaded applications.
- **NUMA balancing disable:** can improve throughput by 5-15% for NUMA-aware applications that already pin memory correctly.
- **THP disable:** eliminates khugepaged-induced latency spikes of 10-100us that appear periodically.
- **Memory pool reuse:** zero shootdowns during steady-state operation, with the one-time cost of pool allocation at startup.

## Caveats

- **Memory consumption:** avoiding `munmap` means physical memory is never returned to the OS. Applications must manage their own memory pools and set appropriate limits.
- **MADV_FREE vs MADV_DONTNEED:** `MADV_DONTNEED` immediately discards pages and zeros them on next access; `MADV_FREE` is lazy. `MADV_DONTNEED` causes an immediate shootdown; `MADV_FREE` defers it.
- **Not all shootdowns are avoidable:** shared library loading, ASLR, and signal handler setup inherently require address space changes. Focus on eliminating shootdowns on hot paths.
- **Container/VM environments:** in virtual environments, TLB shootdowns may be more expensive due to nested page tables (EPT on Intel, NPT on AMD). The optimization is even more impactful in VMs.
- **Kernel version matters:** `MADV_FREE` requires Linux >= 4.5. NUMA balancing behavior varies across kernel versions.
- **ARM specifics:** ARM uses broadcast TLB invalidation (TLBI) instructions which may be cheaper than x86 IPIs on some implementations, but the fundamental cost of cross-core invalidation remains. Apple M-series handles this in hardware more efficiently than server-class ARM cores.
- **Monitoring overhead:** the `/proc/interrupts` approach counts all TLB IPIs system-wide, not per-process. Use `perf trace` for per-process measurement.
