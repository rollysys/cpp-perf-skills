---
name: Huge Pages for TLB Optimization
source: perf-ninja memory_bound/huge_pages_1, perf-book Ch.8
layers: [system]
platforms: [arm, x86]
keywords: [huge pages, TLB, DTLB, madvise, MADV_HUGEPAGE, mmap, MAP_HUGETLB, page fault, 2MB page]
---

## Problem

Any algorithm performing random accesses into a large memory region suffers from DTLB (Data Translation Lookaside Buffer) misses. The TLB caches virtual-to-physical address translations; without it, every memory access requires a costly page walk through the kernel page table (up to 5 levels on modern systems). With the default 4KB page size, a 20MB working set requires 5120 page table entries, far exceeding typical L1 DTLB capacity (e.g., 64 entries on Intel Skylake). Using 2MB huge pages, the same 20MB region needs only 10 entries.

The perf-ninja `huge_pages_1` lab demonstrates this with a finite element matrix-free operator that performs gather-scatter access across large arrays (800 x 20000 nodes, ~122MB of doubles). The memory access pattern is extremely random -- many distant addresses are accessed in rapid succession during `gatherGlobal`/`scatterLocal` operations, putting severe pressure on the TLB:

```cpp
// Random gather: accesses rhs_global at scattered dof indices
auto gatherGlobal(unsigned n1, unsigned n2, const double *rhs_global)
    -> std::array<double, 4> {
  const auto dofs = computeDofs(n1, n2);
  std::array<double, 4> vals;
  for (unsigned i = 0; i < dofs.size(); ++i)
    vals[i] = rhs_global[dofs[i]];  // random access into large array
  return vals;
}
```

With 4KB pages and a 122MB working set, TLB misses dominate execution time because the hardware cannot cache enough translations.

## Detection

**Source-level indicators:**
- Large arrays (>2MB) accessed in random or strided patterns
- Hash tables, binary search trees, sparse matrices, graph traversals on large data
- Finite element gather-scatter, particle-in-cell simulations
- Working sets significantly larger than TLB reach (TLB entries x page size)

**Profile-level indicators:**
- `perf stat -e dtlb_load_misses.walk_completed,dtlb_store_misses.walk_completed`: high DTLB miss counts
- TMA: high `Memory_Bound > L1_Bound > DTLB_Load` or `DTLB_Store` metrics
- `perf stat -e page-faults`: excessive page faults during steady state
- On ARM: `perf stat -e l1d_tlb_refill`: high L1D TLB refill count

**Characteristic symptom:** application that accesses many megabytes of data in a non-sequential pattern and scales poorly despite low cache miss rates at L1/L2/L3.

## Transformation

### Strategy 1: Transparent Huge Pages via madvise (recommended for most applications)

Replace standard allocation with `mmap` + `madvise(MADV_HUGEPAGE)`. This is the approach used in the perf-ninja solution -- only the allocator function changes:

```cpp
// Before: standard allocation with new
inline auto allocateDoublesArray(size_t size) {
  double *alloc = new double[size];
  auto deleter = [](double *ptr) { delete[] ptr; };
  return std::unique_ptr<double[], decltype(deleter)>(alloc, std::move(deleter));
}

// After: mmap + madvise for transparent huge pages (Linux)
#include <sys/mman.h>

inline auto allocateDoublesArray(size_t size) {
  const auto bytes = size * sizeof(double);
  void *raw = mmap(nullptr, bytes, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  if (raw == MAP_FAILED)
    throw std::bad_alloc{};
  madvise(raw, bytes, MADV_HUGEPAGE);
  double *alloc = static_cast<double *>(raw);

  auto deleter = [bytes](double *ptr) { munmap(ptr, bytes); };
  return std::unique_ptr<double[], decltype(deleter)>(alloc, std::move(deleter));
}
```

Requires THP to be enabled: `cat /sys/kernel/mm/transparent_hugepage/enabled` should show `always` or `madvise`.

### Strategy 2: Explicit Huge Pages via MAP_HUGETLB (lowest latency)

Pre-reserve huge pages at boot time and allocate from the reserved pool:

```cpp
#include <sys/mman.h>

void* allocate_explicit_huge(size_t bytes) {
  void *ptr = mmap(nullptr, bytes, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS | MAP_HUGETLB, -1, 0);
  if (ptr == MAP_FAILED)
    throw std::bad_alloc{};
  return ptr;
}
// Requires: echo 128 > /proc/sys/vm/nr_hugepages (reserves 256MB)
```

EHPs cannot be swapped out, eliminating page-fault jitter. Preferred in latency-sensitive contexts (HFT, real-time).

### Strategy 3: System-wide THP for quick experiments

No code changes required. Enable globally and benchmark:

```bash
echo "always" | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
# Run your benchmark, then disable:
echo "madvise" | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
```

The kernel daemon `khugepaged` automatically promotes regular pages to huge pages. Good for initial measurement, not for production deployment.

### Strategy 4: Use jemalloc with THP support

No source code changes needed -- just link or preload:

```bash
# Link at build time:
g++ -o myapp myapp.cpp -ljemalloc

# Or preload at runtime:
LD_PRELOAD=/usr/local/libjemalloc.so.2 MALLOC_CONF="thp:always" ./myapp
```

jemalloc overrides `malloc` and uses THPs for heap allocations transparently.

## Expected Impact

- **SPEC2006 study results:** out of 29 benchmarks, 6 had 1-4% speedup, 4 had 4-8%, 2 had ~10%, and 2 had 22-27% speedup. 15 showed negligible change.
- **Random-access workloads:** 10-30% improvement is typical when DTLB misses are a significant bottleneck.
- **perf-ninja huge_pages_1:** the lab targets measurable improvement on the finite element operator with 122MB working set and random gather-scatter pattern.
- **TLB reach comparison (x86):** with 4KB pages and 64 L1 DTLB entries, TLB reach is 256KB. With 2MB pages and 32 L1 DTLB entries, TLB reach is 64MB -- a 256x improvement.
- **ARM:** similar benefit. Cortex-A72 L1 DTLB has 48 entries for 4KB pages but also supports 2MB pages, dramatically increasing TLB reach.

## Caveats

- **Small working sets don't benefit:** if the entire working set fits within TLB reach using 4KB pages (typically 256KB-1MB), huge pages provide no measurable improvement.
- **Sequential access patterns:** hardware prefetchers and TLB prefetchers handle sequential access well with 4KB pages. Huge pages help most with random or strided access.
- **EHPs require system configuration:** explicit huge pages must be pre-reserved by an administrator. They consume physical RAM even when unused. Not portable across deployments.
- **THP allocation latency:** transparent huge pages can introduce non-deterministic latency as the kernel performs compaction and promotion in the background. Not suitable for ultra-low-latency applications -- use EHPs instead.
- **Memory waste:** huge pages allocate in 2MB granularity. A 2.1MB allocation wastes nearly 1.9MB. Only use for genuinely large allocations.
- **ARM differences:** ARM supports 4KB, 16KB, and 64KB base page sizes depending on kernel configuration. Huge page sizes vary accordingly (2MB or 512MB with 4KB base, 32MB with 16KB base). Check the platform before hardcoding sizes.
- **Windows:** huge pages require `SeLockMemoryPrivilege` and use `VirtualAlloc` with `MEM_LARGE_PAGES`. The API is quite different from Linux. See the perf-ninja `AllocateDoublesArray.hpp` for a complete Windows implementation.
- **No benefit if compute-bound:** if the application is bottlenecked on ALU throughput or instruction cache misses rather than DTLB, huge pages will not help.
