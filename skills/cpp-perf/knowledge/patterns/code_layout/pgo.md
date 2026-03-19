---
name: Profile-Guided Optimization (PGO)
source: perf-ninja misc/pgo
layers: [compiler, toolchain]
platforms: [arm, x86]
keywords: [PGO, profile guided optimization, FDO, feedback directed optimization, profiling, instrumentation, branch weights, code layout]
---

## Problem

Compilers make many heuristic decisions during optimization (branch probabilities, inlining thresholds, code layout, register allocation) that may not match real-world execution patterns. Profile-Guided Optimization feeds actual runtime data back to the compiler, allowing it to make informed decisions.

The perf-ninja PGO lab uses a Lua interpreter where the compiler cannot statically determine which bytecode dispatch paths are hot. PGO tells the compiler exactly which branches are taken, which functions are hot, and how the code should be laid out.

## Detection

- Application has complex control flow (interpreters, state machines, protocol parsers)
- Static heuristics produce suboptimal branch prediction hints
- Performance-critical code paths are not obvious from source code alone
- Application processes specific workload patterns in production
- Large codebase where manual `__builtin_expect` annotation is impractical

## Transformation

**Step 1: Instrumented build** (compile with profiling instrumentation):
```bash
# Clang
clang++ -O2 -fprofile-instr-generate -o myapp_instrumented myapp.cpp

# GCC
g++ -O2 -fprofile-generate -o myapp_instrumented myapp.cpp
```

**Step 2: Run with representative workload** (collect profile data):
```bash
# Run the instrumented binary with typical input
./myapp_instrumented typical_input_1.txt
./myapp_instrumented typical_input_2.txt

# Clang: merge raw profiles
llvm-profdata merge -output=default.profdata *.profraw

# GCC: .gcda files are created automatically
```

**Step 3: Optimized rebuild** (compile using collected profile):
```bash
# Clang
clang++ -O2 -fprofile-instr-use=default.profdata -o myapp_optimized myapp.cpp

# GCC
g++ -O2 -fprofile-use -o myapp_optimized myapp.cpp
```

**CMake integration:**
```cmake
# Profile generation build
if(PGO_GENERATE)
  target_compile_options(myapp PRIVATE -fprofile-instr-generate)
  target_link_options(myapp PRIVATE -fprofile-instr-generate)
elseif(PGO_USE)
  target_compile_options(myapp PRIVATE -fprofile-instr-use=${PGO_PROFILE_PATH})
  target_link_options(myapp PRIVATE -fprofile-instr-use=${PGO_PROFILE_PATH})
endif()
```

## What PGO Optimizes

1. **Branch prediction hints**: hot branches get `likely`/`unlikely` attributes
2. **Code layout**: hot basic blocks placed together for better i-cache utilization
3. **Function inlining**: hot callees inlined more aggressively, cold callees never inlined
4. **Register allocation**: hot paths get more registers
5. **Loop unrolling**: hot loops unrolled more aggressively
6. **Switch lowering**: frequently-hit cases placed first in jump tables

## Expected Impact

- 5-15% speedup for typical applications
- 15-30% for interpreters and complex dispatch-heavy code (like the Lua benchmark)
- Especially effective when combined with LTO (`-flto -fprofile-instr-use=...`)

## Caveats

- Profile data must be representative of production workloads; training on wrong data can cause regressions
- Instrumented binary runs 2-5x slower (unsuitable for production profiling)
- Profile data becomes stale as code changes; rebuild profile periodically
- Build pipeline complexity: requires 3-step build process (instrument, run, optimize)
- AutoFDO (sampling-based) is an alternative that uses `perf` data from production, avoiding instrumented builds:
  ```bash
  perf record -b ./myapp workload
  create_llvm_prof --binary=myapp --out=myapp.prof --profile=perf.data
  clang++ -O2 -fprofile-sample-use=myapp.prof myapp.cpp
  ```
- BOLT (Binary Optimization and Layout Tool) can post-process binaries for similar layout improvements without recompilation
