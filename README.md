# cpp-perf: C++ Performance Optimization Skill for Claude Code

A Claude Code superpowers skill that analyzes and optimizes C++ code for target platforms (ARM/X86). It operates as a 7-stage pipeline: static analysis, optional instrumentation profiling, performance report, benchmark generation, cross-compilation with disassembly analysis, remote execution, and data-driven optimization.

## What It Does

Give it C++ code (snippet, git diff, or file reference) and a target platform. It will:

1. **Analyze** — scan for performance issues across 4 layers (algorithm, language, microarchitecture, system)
2. **Instrument** (optional) — insert lightweight TLS-based timing probes to measure actual hotspots
3. **Report** — grade issues by estimated impact (HIGH/MEDIUM/LOW) with cycle-level estimates
4. **Benchmark** — generate standalone benchmarks, cross-compile, disassemble to verify compiler output
5. **Optimize** — generate optimized code, verify correctness, measure speedup on target hardware
6. **Iterate** — try alternative strategies with clear stopping rules

## Quick Start

```bash
# In a Claude Code session with this skill installed:
> Optimize the performance of my_code.cpp for ARM Cortex-A78
```

The skill will guide you through the full pipeline interactively.

## Project Structure

```
skills/cpp-perf/
├── SKILL.md                    # Trigger metadata
├── cpp-perf.md                 # Pipeline instructions (7 stages)
├── templates/
│   ├── benchmark.cpp.tmpl      # Benchmark harness (steady_clock, JSON, DoNotOptimize)
│   ├── correctness.cpp.tmpl    # Optimization correctness verifier
│   └── cpp_perf_probe.h        # Instrumentation probe (TLS ring buffer, ns timing)
├── profiles/                   # Platform performance profiles (cycles)
│   ├── cortex-a78.yaml
│   ├── cortex-a55.yaml
│   ├── neoverse-n1.yaml
│   └── x86-skylake.yaml
├── knowledge/
│   ├── libraries.yaml          # 25+ high-perf library alternatives
│   └── patterns/               # 14 optimization patterns from references
│       ├── vectorization/      # Auto-vectorization blockers, NEON idioms, SVE
│       ├── memory/             # AoS→SoA, loop tiling, prefetch, false sharing
│       ├── branching/          # Branch→cmov (with counter-examples), lookup tables
│       ├── compute/            # Dependency chains, FMA, strength reduction
│       └── system/             # Huge pages, alignment
└── profiler/                   # C++ hardware profiler
    ├── CMakeLists.txt
    ├── common.h                # Timing, calibration, SIGILL fault tolerance
    ├── main.cpp                # CLI entry point
    ├── output.cpp              # Structured YAML output + CPU model detection
    ├── measure_compute.cpp     # 34 instruction measurements (int/fp/SIMD/LSE/crypto)
    ├── measure_cache.cpp       # Cache hierarchy detection via pointer chasing
    ├── measure_memory.cpp      # Bandwidth, TLB miss penalty
    ├── measure_branch.cpp      # Branch misprediction penalty
    ├── measure_os.cpp          # Syscall, thread, fork, synchronization primitives
    ├── measure_alloc.cpp       # malloc, mmap, page faults
    ├── measure_io.cpp          # File I/O (open/close, read/write, fsync)
    └── measure_ipc.cpp         # Pipe, eventfd, signal, scheduling
```

## Platform Profiler

Generates a platform performance profile by measuring actual hardware characteristics.

```bash
# Build (requires C++17)
cd skills/cpp-perf/profiler
mkdir build && cd build
cmake .. && make -j4

# Run (on target platform)
./profiler > my-board.yaml 2>progress.log

# Run specific measurements only
./profiler compute cache branch
```

Output is a YAML file compatible with the `profiles/` schema. Supports:
- **ARM aarch64** — NEON, DotProd, FP16, LSE atomics, CRC32, AES (with SIGILL fallback for unsupported extensions)
- **x86_64** — SSE, AVX (FMA if available)
- **macOS Apple Silicon** — full support via `mach_absolute_time()` + frequency calibration

## Knowledge Base

14 optimization patterns extracted from professional references:

| Source | Patterns |
|--------|----------|
| [perf-book](https://book.easyperf.net/perf_book) | Vectorization, memory access, branch prediction |
| [perf-ninja](https://github.com/dendibakh/perf-ninja) | Data packing, loop tiling, dependency chains, branchless |
| [ComputeLibrary](https://github.com/ARM-software/ComputeLibrary) | NEON intrinsic idioms |
| [optimized-routines](https://github.com/ARM-software/optimized-routines) | SVE patterns, FMA utilization |
| [Cpp-High-Performance](https://github.com/PacktPublishing/Cpp-High-Performance-Second-Edition) | Strength reduction |

Each pattern includes: problem description, detection method, before/after code, expected impact, and caveats (including when the optimization can make things **worse**).

## Key Design Decisions

- **Cycle-based estimation** with sanity checks — prevents over-confident recommendations (learned from a Game of Life case where "branchless optimization" caused a 3.2x regression)
- **Cross-compilation on host, execution on target** — the skill compiles on your dev machine and runs benchmarks on the ARM board via SSH
- **Disassembly verification** — always checks compiler output before claiming an optimization works
- **Correctness-first** — verifies optimized code matches baseline output before reporting speedup
- **Iterative with stopping rules** — regressions are immediately reverted; <1.2x gains are accepted as "good enough"

## Platform Configuration

On first use, the skill guides you through creating `cpp-perf-platform.yaml`:

```yaml
platforms:
  my-arm-board:
    compiler: aarch64-linux-gnu-g++
    compiler_flags: "-O2 -march=armv8.2-a"
    sysroot: /opt/arm-sysroot        # optional
    host: 192.168.1.100
    port: 22
    user: dev
    arch: aarch64
    work_dir: /tmp/cpp-perf
    profile: cortex-a78
```

## Documentation

- [Design Spec](docs/superpowers/specs/2026-03-19-cpp-perf-skill-design.md) — full architecture and pipeline design
- [Instrumentation Spec](docs/superpowers/specs/2026-03-19-instrumentation-design.md) — TLS probe infrastructure design
- [Plan 1: Core Skill](docs/superpowers/plans/2026-03-19-cpp-perf-plan1-core-skill.md)
- [Plan 2: Knowledge Base](docs/superpowers/plans/2026-03-19-cpp-perf-plan2-knowledge-base.md)
- [Plan 3: Profiler](docs/superpowers/plans/2026-03-19-cpp-perf-plan3-profiler.md)

## License

MIT
