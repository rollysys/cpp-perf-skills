#include "common.h"

#include <fcntl.h>
#include <unistd.h>
#include <cstring>
#include <cerrno>

namespace profiler {

static const char* TEST_FILE = "/tmp/cpp_perf_profiler_test";
static constexpr size_t BUF_4KB = 4096;

// ============================================================
// File I/O measurements
// ============================================================

static double measure_file_open_close() {
    return measure_cycles([&]() {
        int fd = open(TEST_FILE, O_RDWR);
        escape(fd);
        if (fd >= 0) close(fd);
    }, 2000, 100);
}

static double measure_read_4kb() {
    int fd = open(TEST_FILE, O_RDONLY);
    if (fd < 0) return -1.0;

    alignas(64) char buf[BUF_4KB];
    double result = measure_cycles([&]() {
        ssize_t n = pread(fd, buf, BUF_4KB, 0);
        escape(n);
    }, 2000, 100);

    close(fd);
    return result;
}

static double measure_write_4kb() {
    int fd = open(TEST_FILE, O_RDWR);
    if (fd < 0) return -1.0;

    alignas(64) char buf[BUF_4KB];
    memset(buf, 'A', BUF_4KB);

    double result = measure_cycles([&]() {
        ssize_t n = pwrite(fd, buf, BUF_4KB, 0);
        escape(n);
    }, 2000, 100);

    close(fd);
    return result;
}

static double measure_fsync() {
    int fd = open(TEST_FILE, O_RDWR);
    if (fd < 0) return -1.0;

    alignas(64) char buf[BUF_4KB];
    memset(buf, 'B', BUF_4KB);

    double result = measure_cycles([&]() {
        write(fd, buf, BUF_4KB);
        fsync(fd);
    }, 200, 20);

    close(fd);
    return result;
}

// ============================================================
// Entry point
// ============================================================

void measure_io() {
    // Create test file with some initial data
    {
        int fd = open(TEST_FILE, O_CREAT | O_RDWR | O_TRUNC, 0644);
        if (fd < 0) {
            fprintf(stderr, "[io] ERROR: cannot create %s: %s\n",
                    TEST_FILE, strerror(errno));
            return;
        }
        char buf[BUF_4KB];
        memset(buf, 'X', BUF_4KB);
        write(fd, buf, BUF_4KB);
        close(fd);
    }

    record("os_overhead", "file_open_close", measure_file_open_close());

    double rd = measure_read_4kb();
    if (rd > 0) record("os_overhead", "read_4kb", rd);

    double wr = measure_write_4kb();
    if (wr > 0) record("os_overhead", "write_4kb", wr);

    double fs = measure_fsync();
    if (fs > 0) record("os_overhead", "fsync", fs);

    // Clean up test file
    unlink(TEST_FILE);
}

} // namespace profiler
