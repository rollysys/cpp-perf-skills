---
name: Link-Time Optimization (LTO)
source: perf-ninja misc/lto
layers: [compiler, linker]
platforms: [arm, x86]
keywords: [LTO, link time optimization, IPO, interprocedural, cross-TU inlining, whole program optimization]
---

## Problem

When a program is split across multiple translation units (.cpp files), the compiler cannot inline or optimize across file boundaries during normal compilation. Small, frequently-called functions that live in different .cpp files incur full call overhead on every invocation.

The perf-ninja LTO lab demonstrates this with an ambient occlusion renderer split across 6 files:
- `ao_helpers.cpp`: `vdot()`, `vcross()`, `vnormalize()`, `clamp()` -- tiny math functions
- `ao_intersect.cpp`: `ray_sphere_intersect()`, `ray_plane_intersect()` -- calls vdot/vnormalize
- `ao_occlusion.cpp`: `ambient_occlusion()` -- calls intersect functions in tight loop
- `ao_orthoBasis.cpp`, `ao_render.cpp`, `ao_init.cpp`

Without LTO, every call to `vdot()` (a 1-line dot product) from `ray_sphere_intersect()` is a full function call because they are in different .cpp files.

## Detection

- Profile shows hot small functions with high call overhead (prologue/epilogue)
- Functions are defined in separate .cpp files from their callers
- Many small utility/math functions used across translation units
- Program is compiled without `-flto`
- Hot call graph edges cross TU boundaries

## Transformation

**Before** (separate compilation without LTO):
```cmake
# CMakeLists.txt -- normal build
add_executable(ao ao.cpp ao_helpers.cpp ao_intersect.cpp
               ao_occlusion.cpp ao_orthoBasis.cpp ao_render.cpp ao_init.cpp)
target_compile_options(ao PRIVATE -O2)
```

```cpp
// ao_helpers.cpp
double vdot(vec v0, vec v1) {
    return v0.x * v1.x + v0.y * v1.y + v0.z * v1.z;
}

// ao_intersect.cpp -- calls vdot across TU boundary
void ray_sphere_intersect(Isect *isect, const Ray *ray, const Sphere *sphere) {
    // ...
    double B = vdot(rs, ray->dir);  // cannot be inlined without LTO
    double C = vdot(rs, rs) - sphere->radius * sphere->radius;
    // ...
}
```

**After** (enable LTO):
```cmake
# CMakeLists.txt -- with LTO
add_executable(ao ao.cpp ao_helpers.cpp ao_intersect.cpp
               ao_occlusion.cpp ao_orthoBasis.cpp ao_render.cpp ao_init.cpp)
target_compile_options(ao PRIVATE -O2 -flto)
target_link_options(ao PRIVATE -flto)
```

**Compiler flags:**
```bash
# GCC/Clang
-flto                  # Full LTO (highest optimization, slowest link)
-flto=thin             # ThinLTO (Clang only: faster link, nearly same perf)

# MSVC
/GL                    # Compile with whole-program optimization
/LTCG                  # Link with link-time code generation
```

**CMake portable way:**
```cmake
set_property(TARGET myapp PROPERTY INTERPROCEDURAL_OPTIMIZATION TRUE)
```

## Expected Impact

- 10-30% speedup for programs with many cross-TU function calls
- Enables inlining of small functions across translation units
- Enables constant propagation, dead code elimination, and devirtualization across TUs
- The aobench example sees ~20% speedup just from cross-TU inlining of vdot/vnormalize

## Caveats

- LTO significantly increases link time (2-10x slower linking)
- ThinLTO (`-flto=thin`, Clang only) provides most of the benefit with much faster link times
- LTO requires all object files to be compiled with the same compiler and `-flto` flag
- Some build systems and third-party libraries may not support LTO
- Full LTO uses much more memory during linking
- Debug info quality may degrade with LTO; use `-g` at both compile and link stages
- Not a substitute for good code organization; moving hot helpers into headers (inline) achieves the same inlining without LTO overhead
