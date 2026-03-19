#include "common.h"
#include <cstring>

int main(int argc, char* argv[]) {
    bool run_all = (argc == 1);

    auto should_run = [&](const char* name) {
        if (run_all) return true;
        for (int i = 1; i < argc; i++)
            if (strcmp(argv[i], name) == 0) return true;
        return false;
    };

    fprintf(stderr, "cpp-perf profiler starting...\n");

    if (should_run("compute"))  { fprintf(stderr, "[compute] measuring...\n");  profiler::measure_compute(); }
    if (should_run("cache"))    { fprintf(stderr, "[cache] measuring...\n");    profiler::measure_cache(); }
    if (should_run("memory"))   { fprintf(stderr, "[memory] measuring...\n");   profiler::measure_memory(); }
    if (should_run("branch"))   { fprintf(stderr, "[branch] measuring...\n");   profiler::measure_branch(); }
    if (should_run("os"))       { fprintf(stderr, "[os] measuring...\n");       profiler::measure_os(); }
    if (should_run("alloc"))    { fprintf(stderr, "[alloc] measuring...\n");    profiler::measure_alloc(); }
    if (should_run("io"))       { fprintf(stderr, "[io] measuring...\n");       profiler::measure_io(); }
    if (should_run("ipc"))      { fprintf(stderr, "[ipc] measuring...\n");      profiler::measure_ipc(); }

    fprintf(stderr, "done. outputting YAML...\n");
    profiler::output_yaml();
    return 0;
}
