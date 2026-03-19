---
name: Reducing ITLB Misses with Huge Pages for Code
source: perf-book Ch.11, Section 11-8 Reducing ITLB Misses
layers: [system, microarchitecture]
platforms: [arm, x86]
keywords: [ITLB, TLB, huge pages, 2MB pages, page walk, code section, text segment, hugeedit, hugectl, iodlr, THP, transparent huge pages, BOLT hugify, page table, virtual memory]
---

## Problem

The CPU translates virtual instruction addresses to physical addresses via the Instruction TLB (ITLB). When the ITLB cannot serve a translation request, a time-consuming page walk of the kernel page table occurs. For applications with large code sections, this overhead becomes significant.

Key data from the book:
- Intel Golden Cove ITLB can cover up to **1MB** of code. If hot code exceeds this, ITLB misses become a bottleneck.
- The Clang compiler has a `.text` section of ~60MB. ITLB overhead on Intel Coffee Lake is **~7% of cycles** spent doing page walks.
- Applications affected: relational databases (MySQL, PostgreSQL, Oracle), managed runtimes (V8, JVM), cloud services (web search), web tooling (Node.js), compilers, web browsers.

The core issue is that with standard 4KB pages, a 5MB hot code region requires 1280 page table entries. Mapping the same region with 2MB huge pages requires only 3 entries, dramatically reducing ITLB pressure.

## Detection

**TMA metrics:**
- High ITLB overhead in the TMA summary (drill into `Frontend_Bound > Fetch_Latency > ITLB_Misses`)
- `ITLB_Misses` contributing significantly to `Frontend_Bound`

**Hardware performance counters:**

x86 (via `perf stat`):
```
iTLB-load-misses                       # instruction TLB misses
iTLB-loads                             # instruction TLB accesses
# Miss rate = iTLB-load-misses / iTLB-loads
```

ARM (via PMU events):
```
L1I_TLB_REFILL                         # L1 instruction TLB refill
L1I_TLB                                # L1 instruction TLB access
```

**Quick heuristics:**
- Binary `.text` section > 1MB: likely benefits from huge pages
- Non-cold code spread over > 256 4KB pages (> 1MB): ITLB pressure is likely
- Code footprint measurement shows hot code on many 4KB pages with low page utilization

## Transformation

### Method 1: Relink with 2MB page alignment (Linux)

Align the code section to a 2MB boundary at link time, then set the ELF header to request huge page loading:

```bash
# Link with 2MB page alignment
clang++ -Wl,-zcommon-page-size=2097152 -Wl,-zmax-page-size=2097152 \
    -o app app.o

# Permanently set ELF header for huge page loading
hugeedit --text /path/to/app
# Now the code section is loaded using huge pages by default
./app

# Or override at runtime without modifying the binary
hugectl --text ./app
```

**Downside**: The linker inserts up to 2MB of padding to achieve alignment, bloating the binary. For Clang, this increased binary size from 111MB to 114MB.

### Method 2: Runtime remapping with iodlr (Linux, no recompilation)

The Intel [iodlr](https://github.com/intel/iodlr) library allocates huge pages at startup and transfers the code section there. No recompilation or relinking needed:

```bash
# Preload the library -- works with any binary
LD_PRELOAD=/usr/lib64/liblppreload.so ./app
```

Alternatively, call `iodlr` from your `main()` function for tighter integration.

**Advantages over Method 1:**
- No recompilation or relinking required
- Works with preexisting binaries (useful when source is unavailable)
- Works with both explicit and transparent huge pages

### Method 3: BOLT `-hugify` (Linux, profile-guided)

BOLT can inject code to map only hot code onto 2MB pages using Linux Transparent Huge Pages (THP):

```bash
llvm-bolt ./app -o ./app.bolt -data=perf.fdata \
    -hugify \
    -reorder-blocks=ext-tsp -reorder-functions=hfsort \
    -split-functions -split-all-cold
```

**Advantages over Methods 1 and 2:**
- Only hot code is mapped to huge pages (based on profile data), minimizing waste
- Fewer huge pages required, reducing page fragmentation
- Combined with other BOLT optimizations for maximum benefit

## Expected Impact

- **ITLB miss reduction**: Up to **50%** reduction in ITLB misses
- **Performance speedup**: Up to **10%** for large applications with significant ITLB overhead
- **Best candidates**: Applications with `.text` > 1MB and measurable ITLB miss rates
- **Complementary**: Huge pages work on top of other code layout optimizations (PGO, BOLT, function reordering). Standard I-cache optimization techniques (function reordering, function splitting, PGO) also help reduce ITLB pressure indirectly by making hot code more compact.

## Caveats

1. **Small programs waste memory**: Programs with code sections of only a few KB waste the entire 2MB page minus the actual code size. Regular 4KB pages are more memory-efficient for small applications.

2. **Linux-only**: The `hugeedit`, `hugectl`, and `iodlr` approaches described here are Linux-specific. To the author's knowledge, mapping code sections onto huge pages is not currently possible on Windows.

3. **Huge page availability**: Explicit huge pages must be pre-allocated via `/proc/sys/vm/nr_hugepages` or `hugeadm`. Transparent Huge Pages (THP) must be enabled in the kernel. Cloud and container environments may restrict huge page allocation.

4. **Binary size bloat (Method 1)**: Aligning the code section to 2MB boundary can add up to 2MB of padding to the binary.

5. **ARM considerations**: ARM supports multiple page sizes (4KB, 16KB, 64KB base pages; 2MB and 1GB huge pages depending on configuration). The specific mechanism for huge page mapping differs from x86 Linux. Check your platform's kernel configuration and page size support.

6. **Interaction with ASLR**: Address Space Layout Randomization may interact with huge page alignment requirements. Test that ASLR and huge pages work correctly together on your target platform.

7. **Only addresses ITLB misses**: If the primary Frontend bottleneck is I-cache misses rather than ITLB misses, huge pages alone won't help. Use PGO and BOLT to address I-cache issues first; huge pages are the final optimization for ITLB specifically.
