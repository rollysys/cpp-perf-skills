cmake_minimum_required(VERSION 3.10)
project(cpp-perf-profiler LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(profiler
    main.cpp
    output.cpp
    measure_compute.cpp
    measure_cache.cpp
    measure_memory.cpp
    measure_branch.cpp
    measure_os.cpp
    measure_alloc.cpp
    measure_io.cpp
    measure_ipc.cpp
)

target_link_libraries(profiler PRIVATE pthread)

# Cross-compilation: use toolchain file
# cmake -DCMAKE_TOOLCHAIN_FILE=aarch64-toolchain.cmake ..
