---
name: Page Fault Elimination via Pre-faulting
source: perf-book Ch.12, Linux man pages (mlockall, madvise), HFT best practices
layers: [system]
platforms: [arm, x86]
keywords: [page fault, prefault, mlockall, mmap, latency spike, HFT, low latency, deterministic]
---

## Problem

Virtual memory uses demand paging: physical pages are not allocated until first access. Each first touch triggers a **page fault**, which involves a kernel context switch, page table update, and TLB fill. Minor page faults cost 1-10 microseconds; major faults (requiring disk I/O or page clearing) can cost milliseconds.

In latency-sensitive applications (HFT, real-time audio, robotics control loops), page faults during steady-state execution cause unacceptable latency spikes. Even with adequate physical memory, the following scenarios trigger faults:

1. **Heap allocations:** `malloc`/`new` return virtual addresses backed by lazy physical pages. First write to each 4KB page triggers a fault.
2. **Stack growth:** thread stacks grow lazily. Deep call chains or large stack arrays fault on first access to each new page.
3. **Memory-mapped files:** `mmap` regions are demand-paged by default.
4. **glibc memory management:** `free()` may call `munmap()` to return pages to the OS, causing re-faulting on subsequent allocation. `malloc()` uses `mmap()` for large allocations (> 128KB by default).

```cpp
// Problematic: page faults occur during critical trading loop
void on_market_data(const Quote& quote) {
    auto* order = new Order(quote);  // may page fault on heap growth
    process(order);                   // latency spike: 5-50us from page fault
    send_to_exchange(order);
}
```

## Detection

**Source-level indicators:**
- Dynamic memory allocation (`new`, `malloc`, `std::vector::push_back`) on latency-critical paths
- Large stack-allocated arrays in deeply nested functions
- No pre-warming or pre-touching of memory pools at startup
- Missing `mlockall()` or `mlock()` calls in latency-sensitive applications

**Profile-level indicators:**
```bash
# Count page faults during execution
perf stat -e page-faults,minor-faults,major-faults ./myapp

# Trace individual page faults with timestamps
perf record -e page-faults ./myapp
perf script  # shows exact locations and times of faults

# Real-time monitoring
watch -d 'grep -E "(minflt|majflt)" /proc/$(pidof myapp)/stat'
```

**Characteristic symptom:** latency histogram with a long tail -- p50 is 1us but p99 is 50us+, with spikes correlating to page fault events.

## Transformation

### Strategy 1: Pre-fault all allocated memory at startup

Touch every page in all pre-allocated buffers before entering the critical path:

```cpp
#include <cstring>

void prefault_region(void* addr, size_t size) {
    volatile char* p = static_cast<volatile char*>(addr);
    for (size_t i = 0; i < size; i += 4096) {
        p[i] = p[i];  // read+write to trigger fault and dirty the page
    }
}

// At startup:
constexpr size_t POOL_SIZE = 256 * 1024 * 1024;  // 256 MB
void* pool = malloc(POOL_SIZE);
prefault_region(pool, POOL_SIZE);  // all pages now resident
```

### Strategy 2: Lock all pages with mlockall

Prevent the kernel from ever swapping out or reclaiming pages:

```cpp
#include <sys/mman.h>

void lock_all_memory() {
    // MCL_CURRENT: lock all currently mapped pages
    // MCL_FUTURE: lock all pages mapped in the future (new allocations)
    if (mlockall(MCL_CURRENT | MCL_FUTURE) != 0) {
        perror("mlockall failed");
        // Requires CAP_IPC_LOCK or sufficient RLIMIT_MEMLOCK
    }
}
// Call early in main(), before any critical path
```

### Strategy 3: Prevent glibc from returning memory to the OS

glibc's `malloc` uses `mmap` for large allocations and may `munmap` on `free`, causing re-faulting. Disable these behaviors:

```cpp
#include <malloc.h>

void configure_malloc_for_low_latency() {
    // Disable mmap for large allocations -- use brk/sbrk instead
    mallopt(M_MMAP_MAX, 0);

    // Disable heap trimming -- never return memory to OS
    mallopt(M_TRIM_THRESHOLD, -1);

    // Set large mmap threshold to prevent mmap usage
    mallopt(M_MMAP_THRESHOLD, 256 * 1024 * 1024);  // 256 MB
}
```

### Strategy 4: Complete low-latency initialization sequence

Combine all strategies for a production HFT/real-time setup:

```cpp
#include <sys/mman.h>
#include <malloc.h>
#include <sched.h>
#include <pthread.h>

void init_low_latency() {
    // 1. Configure malloc before any allocation
    mallopt(M_MMAP_MAX, 0);
    mallopt(M_TRIM_THRESHOLD, -1);

    // 2. Pre-allocate and pre-fault the memory pool
    constexpr size_t POOL_SIZE = 1ULL << 30;  // 1 GB
    void* pool = malloc(POOL_SIZE);
    prefault_region(pool, POOL_SIZE);

    // 3. Pre-fault the stack for each thread
    // (call from each thread's entry point)
    auto prefault_stack = []() {
        volatile char stack_probe[8 * 1024 * 1024];  // 8 MB
        memset((void*)stack_probe, 0, sizeof(stack_probe));
    };

    // 4. Lock all current and future pages
    mlockall(MCL_CURRENT | MCL_FUTURE);

    // 5. Disable core dumps (prevents CoW page faults)
    // prctl(PR_SET_DUMPABLE, 0);
}
```

### Strategy 5: Use MAP_POPULATE for mmap regions

```cpp
// Pre-fault mmap regions at allocation time
void* region = mmap(nullptr, size, PROT_READ | PROT_WRITE,
                    MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
// MAP_POPULATE: pre-fault all pages immediately (Linux 2.5.46+)
```

## Expected Impact

- **Page fault elimination:** reduces p99 latency from 10-100us to <1us for memory-related operations.
- **Latency tail reduction:** the primary benefit is eliminating latency spikes, not improving average throughput. p99.9 latency can improve by 10-100x.
- **Startup cost trade-off:** pre-faulting 1GB at startup takes ~100-500ms (touching 262144 pages). This one-time cost eliminates all runtime page faults.
- **Deterministic performance:** with all pages locked and pre-faulted, memory access latency becomes deterministic -- no kernel intervention during steady state.

## Caveats

- **Memory overhead:** `mlockall(MCL_FUTURE)` locks ALL future allocations, including those from libraries. Ensure sufficient physical RAM to avoid OOM kills.
- **Requires privileges:** `mlockall` requires `CAP_IPC_LOCK` capability or a sufficient `RLIMIT_MEMLOCK` limit. In production, configure via `/etc/security/limits.conf` or systemd `MemoryLock=infinity`.
- **glibc-specific:** `mallopt` flags are glibc-specific. On musl libc or other allocators (jemalloc, tcmalloc), use their respective configuration mechanisms. jemalloc: `MALLOC_CONF="retain:true"`. tcmalloc: `SetNumericProperty("tcmalloc.aggressive_memory_decommit", 0)`.
- **Stack pre-faulting:** each thread needs its own stack pre-faulted. Use `pthread_attr_setstack` with pre-faulted memory, or touch the stack early in the thread entry function.
- **Transparent Huge Pages interaction:** THP's `khugepaged` can cause latency spikes when it promotes pages. For ultra-low-latency, disable THP (`echo never > /sys/kernel/mm/transparent_hugepage/enabled`) and use explicit huge pages instead.
- **Not a substitute for good allocation patterns:** pre-faulting mitigates the symptom. The root fix is to eliminate dynamic allocation on critical paths entirely -- use memory pools, arena allocators, or pre-allocated ring buffers.
- **macOS/FreeBSD:** `mlockall` exists but `MAP_POPULATE` does not. Use `mlock` on individual regions. `mallopt` is Linux-specific.
