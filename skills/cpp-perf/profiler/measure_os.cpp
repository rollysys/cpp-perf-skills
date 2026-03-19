#include "common.h"

#include <unistd.h>
#include <pthread.h>
#include <atomic>
#include <sys/wait.h>
#include <sched.h>
#include <cstring>

#ifdef __linux__
#include <sys/syscall.h>
#include <linux/futex.h>
#endif

namespace profiler {

// ============================================================
// Process / Thread overhead
// ============================================================

static double measure_syscall() {
    return measure_cycles([&]() {
        auto pid = getpid();
        escape(pid);
    }, 5000, 200);
}

static void* empty_thread_fn(void*) {
    return nullptr;
}

static double measure_thread_create() {
    return measure_cycles([&]() {
        pthread_t t;
        pthread_create(&t, nullptr, empty_thread_fn, nullptr);
        pthread_join(t, nullptr);
    }, 200, 20);
}

static double measure_fork_exit() {
    return measure_cycles([&]() {
        pid_t pid = fork();
        if (pid == 0) {
            _exit(0);
        } else {
            int status;
            waitpid(pid, &status, 0);
        }
    }, 100, 10);
}

#ifdef __linux__
static double measure_cpu_migration() {
    cpu_set_t set;

    // First check that we have at least 2 CPUs
    int ncpus = sysconf(_SC_NPROCESSORS_ONLN);
    if (ncpus < 2) {
        return -1.0;  // Cannot measure migration with only 1 CPU
    }

    return measure_cycles([&]() {
        // Pin to CPU 0
        CPU_ZERO(&set);
        CPU_SET(0, &set);
        sched_setaffinity(0, sizeof(set), &set);

        // Do a tiny bit of work so we are actually on CPU 0
        volatile int dummy = 0;
        for (int i = 0; i < 10; i++) dummy += i;

        // Migrate to CPU 1
        CPU_ZERO(&set);
        CPU_SET(1, &set);
        sched_setaffinity(0, sizeof(set), &set);

        // Force migration by yielding
        sched_yield();

        volatile int dummy2 = 0;
        for (int i = 0; i < 10; i++) dummy2 += i;
    }, 100, 10);
}
#endif

// ============================================================
// Synchronization primitives (all uncontended)
// ============================================================

static double measure_mutex_lock_unlock() {
    pthread_mutex_t mtx = PTHREAD_MUTEX_INITIALIZER;
    double result = measure_cycles([&]() {
        pthread_mutex_lock(&mtx);
        pthread_mutex_unlock(&mtx);
    }, 5000, 200);
    pthread_mutex_destroy(&mtx);
    return result;
}

static double measure_spinlock() {
    std::atomic_flag flag = ATOMIC_FLAG_INIT;
    return measure_cycles([&]() {
        while (flag.test_and_set(std::memory_order_acquire)) {}
        flag.clear(std::memory_order_release);
    }, 5000, 200);
}

static double measure_rwlock_read() {
    pthread_rwlock_t rwl = PTHREAD_RWLOCK_INITIALIZER;
    double result = measure_cycles([&]() {
        pthread_rwlock_rdlock(&rwl);
        pthread_rwlock_unlock(&rwl);
    }, 5000, 200);
    pthread_rwlock_destroy(&rwl);
    return result;
}

static double measure_rwlock_write() {
    pthread_rwlock_t rwl = PTHREAD_RWLOCK_INITIALIZER;
    double result = measure_cycles([&]() {
        pthread_rwlock_wrlock(&rwl);
        pthread_rwlock_unlock(&rwl);
    }, 5000, 200);
    pthread_rwlock_destroy(&rwl);
    return result;
}

#ifdef __linux__
static double measure_futex() {
    int futex_word = 0;
    return measure_cycles([&]() {
        // FUTEX_WAKE with 0 waiters — just measures the syscall round-trip
        syscall(SYS_futex, &futex_word, FUTEX_WAKE, 1, nullptr, nullptr, 0);
    }, 5000, 200);
}
#endif

static double measure_atomic_seq_cst() {
    std::atomic<int> val{0};
    return measure_cycles([&]() {
        val.fetch_add(1, std::memory_order_seq_cst);
    }, 5000, 200);
}

static double measure_atomic_acq_rel() {
    std::atomic<int> val{0};
    return measure_cycles([&]() {
        val.fetch_add(1, std::memory_order_acq_rel);
    }, 5000, 200);
}

static double measure_atomic_relaxed() {
    std::atomic<int> val{0};
    return measure_cycles([&]() {
        val.fetch_add(1, std::memory_order_relaxed);
    }, 5000, 200);
}

// ============================================================
// Entry point
// ============================================================

void measure_os() {
    // Process / Thread
    record("os_overhead", "syscall", measure_syscall());
    record("os_overhead", "thread_create", measure_thread_create());
    record("os_overhead", "fork", measure_fork_exit());

#ifdef __linux__
    double mig = measure_cpu_migration();
    if (mig > 0) {
        record("os_overhead", "cpu_migration", mig);
    }
#endif

    // Synchronization
    record("os_overhead", "mutex_lock_unlock", measure_mutex_lock_unlock());
    record("os_overhead", "spinlock", measure_spinlock());
    record("os_overhead", "rwlock_read", measure_rwlock_read());
    record("os_overhead", "rwlock_write", measure_rwlock_write());

#ifdef __linux__
    record("os_overhead", "futex", measure_futex());
#endif

    record("os_overhead", "atomic_seq_cst", measure_atomic_seq_cst());
    record("os_overhead", "atomic_acq_rel", measure_atomic_acq_rel());
    record("os_overhead", "atomic_relaxed", measure_atomic_relaxed());
}

} // namespace profiler
