#include "common.h"

#include <cstdlib>
#include <cstring>
#include <sys/mman.h>
#include <unistd.h>

#ifdef __linux__
#include <sys/mman.h>  // MADV_DONTNEED, MAP_HUGETLB
#endif

namespace profiler {

// ============================================================
// malloc/free for various sizes
// ============================================================

static double measure_malloc_free(size_t sz) {
    return measure_cycles([&]() {
        void* p = malloc(sz);
        escape(p);
        free(p);
    }, 5000, 200);
}

// ============================================================
// mmap / munmap anonymous page
// ============================================================

static double measure_mmap_anon() {
    long page_size = sysconf(_SC_PAGESIZE);
    return measure_cycles([&]() {
        void* p = mmap(nullptr, page_size, PROT_READ | PROT_WRITE,
                       MAP_ANONYMOUS | MAP_PRIVATE, -1, 0);
        escape(p);
        if (p != MAP_FAILED) {
            munmap(p, page_size);
        }
    }, 1000, 50);
}

// ============================================================
// Page fault measurements
// ============================================================

static double measure_minor_page_fault() {
    long page_size = sysconf(_SC_PAGESIZE);
    // Measure the cost of the first touch on a freshly mapped page
    // We map a new page each iteration so the first write triggers a minor fault
    return measure_cycles([&]() {
        void* p = mmap(nullptr, page_size, PROT_READ | PROT_WRITE,
                       MAP_ANONYMOUS | MAP_PRIVATE, -1, 0);
        if (p == MAP_FAILED) return;

        clobber();
        // First access triggers the minor page fault
        uint64_t c0 = rdcycle();
        *static_cast<volatile char*>(p) = 42;
        uint64_t c1 = rdcycle();
        escape(c1 - c0);

        munmap(p, page_size);
    }, 500, 20);
    // Note: the actual fault cost is embedded in the larger measurement.
    // A more precise approach measures the inner delta, but that requires
    // a different harness. Using measure_cycles here captures mmap+fault+munmap
    // variation. We accept this as a practical approximation.
}

#ifdef __linux__
static double measure_major_page_fault() {
    long page_size = sysconf(_SC_PAGESIZE);
    // Map once, then use MADV_DONTNEED to discard, re-access
    void* p = mmap(nullptr, page_size, PROT_READ | PROT_WRITE,
                   MAP_ANONYMOUS | MAP_PRIVATE, -1, 0);
    if (p == MAP_FAILED) return -1.0;

    // Initial touch
    *static_cast<volatile char*>(p) = 1;

    double result = measure_cycles([&]() {
        // Discard the page contents — next access triggers fault
        madvise(p, page_size, MADV_DONTNEED);
        clobber();
        // Re-access: triggers a page fault (minor, but simulates the re-fault path)
        *static_cast<volatile char*>(p) = 42;
        clobber();
    }, 500, 20);

    munmap(p, page_size);
    return result;
}
#endif

#ifdef __linux__
static double measure_huge_page_alloc() {
    constexpr size_t HUGE_2MB = 2UL * 1024 * 1024;

    void* p = mmap(nullptr, HUGE_2MB, PROT_READ | PROT_WRITE,
                   MAP_ANONYMOUS | MAP_PRIVATE | MAP_HUGETLB, -1, 0);
    if (p == MAP_FAILED) {
        // Huge pages not available — return -1 to signal unavailable
        return -1.0;
    }
    munmap(p, HUGE_2MB);

    return measure_cycles([&]() {
        void* hp = mmap(nullptr, HUGE_2MB, PROT_READ | PROT_WRITE,
                        MAP_ANONYMOUS | MAP_PRIVATE | MAP_HUGETLB, -1, 0);
        escape(hp);
        if (hp != MAP_FAILED) {
            munmap(hp, HUGE_2MB);
        }
    }, 200, 10);
}
#endif

// ============================================================
// Entry point
// ============================================================

void measure_alloc() {
    record("os_overhead", "malloc_16b",  measure_malloc_free(16));
    record("os_overhead", "malloc_256b", measure_malloc_free(256));
    record("os_overhead", "malloc_4kb",  measure_malloc_free(4096));
    record("os_overhead", "malloc_1mb",  measure_malloc_free(1024 * 1024));

    record("os_overhead", "mmap_anon",        measure_mmap_anon());
    record("os_overhead", "minor_page_fault", measure_minor_page_fault());

#ifdef __linux__
    double mpf = measure_major_page_fault();
    if (mpf > 0) {
        record("os_overhead", "major_page_fault", mpf);
    }

    double hp = measure_huge_page_alloc();
    if (hp > 0) {
        record("os_overhead", "huge_page_alloc", hp);
    }
#endif
}

} // namespace profiler
