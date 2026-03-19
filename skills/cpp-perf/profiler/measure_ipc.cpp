#include "common.h"

#include <unistd.h>
#include <pthread.h>
#include <signal.h>
#include <sched.h>
#include <cstring>
#include <sys/wait.h>
#include <time.h>

#ifdef __linux__
#include <sys/eventfd.h>
#endif

namespace profiler {

// ============================================================
// IPC measurements
// ============================================================

static double measure_pipe_roundtrip() {
    int pipe_ab[2]; // parent -> child thread
    int pipe_ba[2]; // child thread -> parent
    if (pipe(pipe_ab) < 0 || pipe(pipe_ba) < 0) return -1.0;

    struct PipeArgs {
        int read_fd;
        int write_fd;
    };
    PipeArgs args = { pipe_ab[0], pipe_ba[1] };

    auto thread_fn = [](void* arg) -> void* {
        auto* a = static_cast<PipeArgs*>(arg);
        char buf[64];
        // Echo loop: read from parent, write back
        while (true) {
            ssize_t n = read(a->read_fd, buf, 64);
            if (n <= 0) break;
            write(a->write_fd, buf, n);
        }
        return nullptr;
    };

    pthread_t t;
    pthread_create(&t, nullptr, thread_fn, &args);

    char send_buf[64];
    char recv_buf[64];
    memset(send_buf, 'P', 64);

    double result = measure_cycles([&]() {
        write(pipe_ab[1], send_buf, 64);
        ssize_t n = read(pipe_ba[0], recv_buf, 64);
        escape(n);
    }, 1000, 50);

    // Shut down the thread by closing the write end
    close(pipe_ab[1]);
    pthread_join(t, nullptr);

    close(pipe_ab[0]);
    close(pipe_ba[0]);
    close(pipe_ba[1]);

    return result;
}

#ifdef __linux__
static double measure_eventfd_roundtrip() {
    int efd_to_child = eventfd(0, 0);
    int efd_to_parent = eventfd(0, 0);
    if (efd_to_child < 0 || efd_to_parent < 0) return -1.0;

    struct EvArgs {
        int read_efd;
        int write_efd;
        volatile bool stop;
    };
    EvArgs args = { efd_to_child, efd_to_parent, false };

    auto thread_fn = [](void* arg) -> void* {
        auto* a = static_cast<EvArgs*>(arg);
        uint64_t val;
        while (!a->stop) {
            if (read(a->read_efd, &val, sizeof(val)) <= 0) break;
            val = 1;
            write(a->write_efd, &val, sizeof(val));
        }
        return nullptr;
    };

    pthread_t t;
    pthread_create(&t, nullptr, thread_fn, &args);

    double result = measure_cycles([&]() {
        uint64_t val = 1;
        write(efd_to_child, &val, sizeof(val));
        read(efd_to_parent, &val, sizeof(val));
        escape(val);
    }, 1000, 50);

    args.stop = true;
    uint64_t wake = 1;
    write(efd_to_child, &wake, sizeof(wake)); // unblock thread
    pthread_join(t, nullptr);

    close(efd_to_child);
    close(efd_to_parent);
    return result;
}
#endif

// Signal delivery measurement
static volatile uint64_t signal_handler_entry_cycle = 0;

static void sigusr1_handler(int) {
    signal_handler_entry_cycle = rdcycle();
}

static double measure_signal_delivery() {
    struct sigaction sa, old_sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = sigusr1_handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGUSR1, &sa, &old_sa);

    pid_t self = getpid();

    std::vector<double> samples;
    samples.reserve(1000);

    // Warmup
    for (int i = 0; i < 50; i++) {
        kill(self, SIGUSR1);
    }

    for (int i = 0; i < 1000; i++) {
        signal_handler_entry_cycle = 0;
        clobber();
        uint64_t c0 = rdcycle();
        kill(self, SIGUSR1);
        uint64_t c_handler = signal_handler_entry_cycle;
        // Measure time from kill() to handler entry
        if (c_handler > c0) {
            samples.push_back((double)(c_handler - c0));
        }
    }

    // Restore old handler
    sigaction(SIGUSR1, &old_sa, nullptr);

    if (samples.empty()) return -1.0;
    auto stats = compute_stats(samples);
    return stats.median;
}

// ============================================================
// Scheduling measurements
// ============================================================

static double measure_sched_yield() {
    return measure_cycles([&]() {
        sched_yield();
    }, 5000, 200);
}

static double measure_timer_resolution_ns() {
    // Measure the minimum observable delta from clock_gettime(CLOCK_MONOTONIC)
    std::vector<double> samples;
    samples.reserve(10000);

    // Warmup
    struct timespec ts;
    for (int i = 0; i < 100; i++) {
        clock_gettime(CLOCK_MONOTONIC, &ts);
    }

    for (int i = 0; i < 10000; i++) {
        struct timespec t0, t1;
        clock_gettime(CLOCK_MONOTONIC, &t0);
        // Keep calling until we see a different value
        do {
            clock_gettime(CLOCK_MONOTONIC, &t1);
        } while (t0.tv_sec == t1.tv_sec && t0.tv_nsec == t1.tv_nsec);

        long long delta_ns = (t1.tv_sec - t0.tv_sec) * 1000000000LL
                           + (t1.tv_nsec - t0.tv_nsec);
        samples.push_back((double)delta_ns);
    }

    auto stats = compute_stats(samples);
    return stats.median; // in nanoseconds
}

static double measure_context_switch() {
    // Pipe ping-pong between parent and forked child.
    // Each round trip crosses two context switches (parent->child, child->parent).
    // We divide by 2 to get per-switch cost.
    int pipe_pc[2]; // parent -> child
    int pipe_cp[2]; // child -> parent
    if (pipe(pipe_pc) < 0 || pipe(pipe_cp) < 0) return -1.0;

    pid_t pid = fork();
    if (pid < 0) return -1.0;

    if (pid == 0) {
        // Child: echo loop
        close(pipe_pc[1]);
        close(pipe_cp[0]);
        char buf[1];
        while (read(pipe_pc[0], buf, 1) > 0) {
            write(pipe_cp[1], buf, 1);
        }
        close(pipe_pc[0]);
        close(pipe_cp[1]);
        _exit(0);
    }

    // Parent
    close(pipe_pc[0]);
    close(pipe_cp[1]);

    char send_buf = 'X';
    char recv_buf;

    // Warmup
    for (int i = 0; i < 50; i++) {
        write(pipe_pc[1], &send_buf, 1);
        read(pipe_cp[0], &recv_buf, 1);
    }

    std::vector<double> samples;
    samples.reserve(1000);

    for (int i = 0; i < 1000; i++) {
        uint64_t c0 = rdcycle();
        write(pipe_pc[1], &send_buf, 1);
        read(pipe_cp[0], &recv_buf, 1);
        uint64_t c1 = rdcycle();
        // Round trip = 2 context switches + 2 pipe transfers
        // We record the full round trip; user can compare to pipe_roundtrip
        // to isolate context switch cost
        samples.push_back((double)(c1 - c0));
    }

    // Shut down child
    close(pipe_pc[1]);
    int status;
    waitpid(pid, &status, 0);
    close(pipe_cp[0]);

    auto stats = compute_stats(samples);
    return stats.median;
}

// ============================================================
// Entry point
// ============================================================

void measure_ipc() {
    // IPC
    double pr = measure_pipe_roundtrip();
    if (pr > 0) record("os_overhead", "pipe_roundtrip", pr);

#ifdef __linux__
    double er = measure_eventfd_roundtrip();
    if (er > 0) record("os_overhead", "eventfd_roundtrip", er);
#endif

    double sig = measure_signal_delivery();
    if (sig > 0) record("os_overhead", "signal_delivery", sig);

    // Scheduling
    record("os_overhead", "sched_yield", measure_sched_yield());
    record("os_overhead", "timer_resolution_ns", measure_timer_resolution_ns());

    double cs = measure_context_switch();
    if (cs > 0) record("os_overhead", "context_switch", cs);
}

} // namespace profiler
