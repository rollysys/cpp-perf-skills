# Reference Materials

## Documents

- `arm_cortex_a78_core_trm_101430_0102_09_en.pdf` — Arm Cortex-A78 Core Technical Reference Manual
- `Arm_Cortex-A78_Core_Software_Optimization_Guide.pdf` — Arm Cortex-A78 Software Optimization Guide

## Open Source Repositories

Clone locally to `reference/` for offline use. These directories are gitignored.

| Directory | Repository | Description |
|---|---|---|
| `abseil-cpp/` | https://github.com/abseil/abseil-cpp | Google's C++ library — high-performance containers and utilities |
| `ComputeLibrary/` | https://github.com/ARM-software/ComputeLibrary | ARM Compute Library — NEON/SVE optimized ML primitives |
| `Cpp-High-Performance/` | https://github.com/PacktPublishing/Cpp-High-Performance | Book code: C++ High Performance |
| `MegPeak/` | https://github.com/MegEngine/MegPeak | CPU peak performance measurement tool |
| `optimized-routines/` | https://github.com/ARM-software/optimized-routines | ARM optimized string/math routines |
| `perf-book/` | https://github.com/dendibakh/perf-book | Performance Analysis and Tuning on Modern CPUs |
| `perf-ninja/` | https://github.com/dendibakh/perf-ninja | Performance optimization coding exercises |
| `perf-tools/` | https://github.com/brendangregg/perf-tools | Brendan Gregg's perf-tools collection |

To clone all:

```bash
cd reference
git clone --depth 1 https://github.com/abseil/abseil-cpp.git
git clone --depth 1 https://github.com/ARM-software/ComputeLibrary.git
git clone --depth 1 https://github.com/PacktPublishing/Cpp-High-Performance.git
git clone --depth 1 https://github.com/MegEngine/MegPeak.git
git clone --depth 1 https://github.com/ARM-software/optimized-routines.git
git clone --depth 1 https://github.com/dendibakh/perf-book.git
git clone --depth 1 https://github.com/dendibakh/perf-ninja.git
git clone --depth 1 https://github.com/brendangregg/perf-tools.git
```
