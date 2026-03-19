// cpp_perf_probe.h — Self-contained instrumentation profiling header
// Part of the cpp-perf skill. No external dependencies beyond standard library.
// Usage: #include "cpp_perf_probe.h", insert PROBE_SCOPE / PROBE_BEGIN / PROBE_END,
//        call profiler::probe_report() after all threads joined.
//
// Compile with: -std=c++17 -Wall -Wextra
#ifndef CPP_PERF_PROBE_H
#define CPP_PERF_PROBE_H

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <algorithm>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace profiler {

// ============================================================
// Timing source — nanosecond timestamps
// ============================================================

#if defined(__x86_64__) || defined(_M_X64)
inline uint64_t tsc_freq() {
    static uint64_t freq = []() -> uint64_t {
        struct timespec t0{};
        struct timespec t1{};
        clock_gettime(CLOCK_MONOTONIC_RAW, &t0);
        uint32_t lo0, hi0, lo1, hi1;
        asm volatile("lfence\n\trdtsc" : "=a"(lo0), "=d"(hi0));
        // Spin ~10ms for calibration
        for (;;) {
            clock_gettime(CLOCK_MONOTONIC_RAW, &t1);
            int64_t elapsed = (t1.tv_sec - t0.tv_sec) * 1000000000LL
                            + (t1.tv_nsec - t0.tv_nsec);
            if (elapsed >= 10000000) break;
        }
        asm volatile("lfence\n\trdtsc" : "=a"(lo1), "=d"(hi1));
        uint64_t dt_ns = static_cast<uint64_t>(
            (t1.tv_sec - t0.tv_sec) * 1000000000LL + (t1.tv_nsec - t0.tv_nsec));
        uint64_t dt_tsc = (static_cast<uint64_t>(hi1) << 32 | lo1)
                        - (static_cast<uint64_t>(hi0) << 32 | lo0);
        return static_cast<uint64_t>(
            static_cast<double>(dt_tsc) / static_cast<double>(dt_ns) * 1e9);
    }();
    return freq;
}
#endif

inline uint64_t probe_timestamp_ns() {
#if defined(__aarch64__)
    uint64_t val;
    asm volatile("mrs %0, cntvct_el0" : "=r"(val));
    // Cache the counter frequency — it never changes at runtime.
    static uint64_t freq = []() -> uint64_t {
        uint64_t f;
        asm volatile("mrs %0, cntfrq_el0" : "=r"(f));
        return f;
    }();
    return static_cast<uint64_t>(
        static_cast<__uint128_t>(val) * 1000000000ULL / freq);
#elif defined(__x86_64__) || defined(_M_X64)
    uint32_t lo, hi;
    asm volatile("lfence\n\trdtsc" : "=a"(lo), "=d"(hi));
    uint64_t tsc = static_cast<uint64_t>(hi) << 32 | lo;
    return static_cast<uint64_t>(
        static_cast<__uint128_t>(tsc) * 1000000000ULL / tsc_freq());
#else
    struct timespec ts{};
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return static_cast<uint64_t>(ts.tv_sec) * 1000000000ULL
         + static_cast<uint64_t>(ts.tv_nsec);
#endif
}

// ============================================================
// Data structures
// ============================================================

struct ProbeEvent {
    uint32_t probe_id;      // Probe identifier
    uint32_t flags;          // 0 = BEGIN, 1 = END
    uint64_t timestamp_ns;   // Nanoseconds
};

// 128K events = 2MB per thread
inline constexpr uint32_t PROBE_BUF_SIZE = 131072;

struct alignas(64) ProbeBuffer {
    ProbeEvent events[PROBE_BUF_SIZE];
    uint32_t write_pos       = 0;
    uint32_t thread_id       = 0;
    uint32_t overflow_count  = 0;
};

inline thread_local ProbeBuffer tls_probe_buf{};

// ============================================================
// Multi-thread buffer registration
// ============================================================

inline std::vector<ProbeBuffer*>& all_buffers() {
    static std::vector<ProbeBuffer*> bufs;
    return bufs;
}

inline std::mutex& buf_mutex() {
    static std::mutex mtx;
    return mtx;
}

struct ProbeBufferRegistrar {
    ProbeBufferRegistrar() {
        std::lock_guard<std::mutex> lk(buf_mutex());
        tls_probe_buf.thread_id = static_cast<uint32_t>(all_buffers().size());
        all_buffers().push_back(&tls_probe_buf);
    }
};

// Force registration on each thread that touches this header.
inline thread_local ProbeBufferRegistrar tls_registrar{};

// ============================================================
// Probe name registry
// ============================================================

struct ProbeInfo {
    std::string name;
    std::string file;
    int         line = 0;
};

inline std::unordered_map<uint32_t, ProbeInfo>& probe_registry() {
    static std::unordered_map<uint32_t, ProbeInfo> reg;
    return reg;
}

inline void probe_register(uint32_t id, const char* name,
                            const char* file, int line) {
    probe_registry()[id] = {name, file, line};
}

#define PROBE_REGISTER(id, name) \
    profiler::probe_register(id, name, __FILE__, __LINE__)

// ============================================================
// Probe API — hot path
// ============================================================

inline void probe_mark(uint32_t id, uint32_t flag) {
    // Access the registrar to ensure TLS buffer is registered on this thread.
    (void)tls_registrar;
    auto& buf = tls_probe_buf;
    if (buf.write_pos >= PROBE_BUF_SIZE) {
        buf.overflow_count++;
        buf.write_pos = 0; // Wrap (ring buffer — overwrites oldest data)
    }
    buf.events[buf.write_pos] = {id, flag, probe_timestamp_ns()};
    buf.write_pos++;
}

#define PROBE_BEGIN(id) profiler::probe_mark(id, 0)
#define PROBE_END(id)   profiler::probe_mark(id, 1)

struct ScopeProbe {
    uint32_t id;
    explicit ScopeProbe(uint32_t probe_id) : id(probe_id) {
        probe_mark(id, 0);
    }
    ~ScopeProbe() { probe_mark(id, 1); }
    ScopeProbe(const ScopeProbe&)            = delete;
    ScopeProbe& operator=(const ScopeProbe&) = delete;
};

#define PROBE_SCOPE(id) profiler::ScopeProbe _probe_##id(id)

// ============================================================
// Report generation
// ============================================================

namespace detail {

struct ProbeStats {
    uint32_t probe_id    = 0;
    uint32_t parent_id   = 0;   // 0 means root
    uint64_t calls       = 0;
    uint64_t total_ns    = 0;
    uint64_t self_ns     = 0;
    double   pct_of_parent = 0.0;
    bool     is_root     = true;
};

// Escape a string for JSON output (handles \, ", control chars)
inline void json_escape(const char* s, std::string& out) {
    for (; *s; ++s) {
        switch (*s) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:
                if (static_cast<unsigned char>(*s) < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x",
                                  static_cast<unsigned>(*s));
                    out += buf;
                } else {
                    out += *s;
                }
        }
    }
}

struct TreeNode {
    uint32_t              probe_id = 0;
    uint64_t              calls    = 0;
    uint64_t              total_ns = 0;
    uint64_t              self_ns  = 0;
    double                pct      = 0.0;
    std::vector<TreeNode> children;
};

inline void build_tree(TreeNode& node,
                       const std::unordered_map<uint32_t, ProbeStats>& stats,
                       const std::unordered_map<uint32_t,
                           std::vector<uint32_t>>& children_map) {
    auto it = children_map.find(node.probe_id);
    if (it == children_map.end()) return;
    for (uint32_t child_id : it->second) {
        auto sit = stats.find(child_id);
        if (sit == stats.end()) continue;
        const auto& cs = sit->second;
        TreeNode child;
        child.probe_id = cs.probe_id;
        child.calls    = cs.calls;
        child.total_ns = cs.total_ns;
        child.self_ns  = cs.self_ns;
        child.pct      = cs.pct_of_parent;
        build_tree(child, stats, children_map);
        node.children.push_back(std::move(child));
    }
}

inline void emit_tree_json(const TreeNode& node, std::string& out,
                            int indent) {
    auto pad = [&](int extra = 0) {
        for (int i = 0; i < indent + extra; ++i) out += "  ";
    };

    pad(); out += "{\n";
    pad(1); out += "\"probe_id\": ";
    out += std::to_string(node.probe_id); out += ",\n";
    pad(1); out += "\"calls\": ";
    out += std::to_string(node.calls); out += ",\n";
    pad(1); out += "\"total_ns\": ";
    out += std::to_string(node.total_ns); out += ",\n";
    pad(1); out += "\"self_ns\": ";
    out += std::to_string(node.self_ns); out += ",\n";

    char pct_buf[32];
    std::snprintf(pct_buf, sizeof(pct_buf), "%.1f", node.pct);
    pad(1); out += "\"pct_of_parent\": ";
    out += pct_buf;

    if (!node.children.empty()) {
        out += ",\n";
        pad(1); out += "\"children\": [\n";
        for (size_t i = 0; i < node.children.size(); ++i) {
            emit_tree_json(node.children[i], out, indent + 2);
            if (i + 1 < node.children.size()) out += ",";
            out += "\n";
        }
        pad(1); out += "]\n";
    } else {
        out += "\n";
    }
    pad(); out += "}";
}

} // namespace detail

inline void probe_report() {
    // ---- Step 0: Collect warnings ----
    std::vector<std::string> warnings;
    const auto& buffers = all_buffers();

    // ---- Step 1: Merge all thread buffers ----
    struct TaggedEvent {
        uint32_t probe_id;
        uint32_t flags;
        uint64_t timestamp_ns;
        uint32_t thread_id;
    };
    std::vector<TaggedEvent> merged;

    for (const ProbeBuffer* buf : buffers) {
        if (buf->overflow_count > 0) {
            char warn[128];
            std::snprintf(warn, sizeof(warn),
                "thread %u buffer overflowed %u times, results may be incomplete",
                buf->thread_id, buf->overflow_count);
            warnings.emplace_back(warn);
            std::fprintf(stderr,
                "warning: thread %u buffer overflowed %u times, "
                "results may be incomplete\n",
                buf->thread_id, buf->overflow_count);
        }
        uint32_t count = buf->write_pos < PROBE_BUF_SIZE
                       ? buf->write_pos : PROBE_BUF_SIZE;
        for (uint32_t i = 0; i < count; ++i) {
            const auto& ev = buf->events[i];
            merged.push_back({ev.probe_id, ev.flags,
                              ev.timestamp_ns, buf->thread_id});
        }
    }

    std::sort(merged.begin(), merged.end(),
        [](const TaggedEvent& a, const TaggedEvent& b) {
            return a.timestamp_ns < b.timestamp_ns;
        });

    // ---- Step 2: Build per-probe stats using stack-based matching ----
    // Group events by thread, preserving timestamp order.
    std::unordered_map<uint32_t, std::vector<const TaggedEvent*>> by_thread;
    for (const auto& ev : merged) {
        by_thread[ev.thread_id].push_back(&ev);
    }

    // Accumulated stats: probe_id -> stats
    std::unordered_map<uint32_t, detail::ProbeStats> stats;
    // Parent-child relationships: parent_id -> [child_ids]
    std::unordered_map<uint32_t, std::vector<uint32_t>> children_map;

    for (auto& [tid, events] : by_thread) {
        struct StackFrame {
            uint32_t probe_id;
            uint64_t begin_ts;
        };
        std::vector<StackFrame> stack;

        for (const auto* ev : events) {
            if (ev->flags == 0) {
                // BEGIN
                stack.push_back({ev->probe_id, ev->timestamp_ns});
            } else {
                // END
                if (stack.empty() || stack.back().probe_id != ev->probe_id) {
                    // Orphaned END — skip
                    continue;
                }
                StackFrame frame = stack.back();
                stack.pop_back();

                uint64_t duration = ev->timestamp_ns - frame.begin_ts;
                uint32_t parent_id = stack.empty()
                                   ? 0u : stack.back().probe_id;

                auto& s        = stats[frame.probe_id];
                s.probe_id     = frame.probe_id;
                s.calls       += 1;
                s.total_ns    += duration;

                // Track parent. If a probe appears under multiple parents,
                // the last one wins (consistent within a single call tree).
                s.parent_id = parent_id;
                if (parent_id != 0) {
                    s.is_root = false;
                }

                // Register parent-child edge (deduplicated later)
                if (parent_id != 0) {
                    auto& ch = children_map[parent_id];
                    if (std::find(ch.begin(), ch.end(), frame.probe_id)
                            == ch.end()) {
                        ch.push_back(frame.probe_id);
                    }
                } else {
                    // Ensure root entries appear in children_map[0]
                    auto& ch = children_map[0];
                    if (std::find(ch.begin(), ch.end(), frame.probe_id)
                            == ch.end()) {
                        ch.push_back(frame.probe_id);
                    }
                }
            }
        }
        // Any remaining items on the stack are orphaned BEGINs — discard.
    }

    // ---- Step 3: Compute self_ns ----
    for (auto& [id, s] : stats) {
        uint64_t children_total = 0;
        auto it = children_map.find(id);
        if (it != children_map.end()) {
            for (uint32_t child_id : it->second) {
                auto cit = stats.find(child_id);
                if (cit != stats.end()) {
                    children_total += cit->second.total_ns;
                }
            }
        }
        s.self_ns = (s.total_ns >= children_total)
                  ? s.total_ns - children_total : 0;
    }

    // ---- Step 4: Compute pct_of_parent ----
    for (auto& [id, s] : stats) {
        if (s.parent_id == 0) {
            s.pct_of_parent = 100.0;
        } else {
            auto pit = stats.find(s.parent_id);
            if (pit != stats.end() && pit->second.total_ns > 0) {
                s.pct_of_parent = static_cast<double>(s.total_ns)
                    / static_cast<double>(pit->second.total_ns) * 100.0;
            } else {
                s.pct_of_parent = 0.0;
            }
        }
    }

    // ---- Step 5: Build JSON tree and output ----
    std::string json;
    json.reserve(4096);
    json += "{\n";
    json += "  \"timestamp_unit\": \"nanoseconds\",\n";

    // Warnings
    json += "  \"warnings\": [";
    for (size_t i = 0; i < warnings.size(); ++i) {
        json += "\"";
        detail::json_escape(warnings[i].c_str(), json);
        json += "\"";
        if (i + 1 < warnings.size()) json += ", ";
    }
    json += "],\n";

    // Probe registry
    json += "  \"probes\": {";
    {
        const auto& reg = probe_registry();
        bool first = true;
        // Sort by ID for deterministic output
        std::vector<uint32_t> reg_ids;
        reg_ids.reserve(reg.size());
        for (const auto& [rid, info] : reg) {
            (void)info;
            reg_ids.push_back(rid);
        }
        std::sort(reg_ids.begin(), reg_ids.end());
        for (uint32_t rid : reg_ids) {
            const auto& info = reg.at(rid);
            if (!first) json += ",";
            first = false;
            json += "\n    \"";
            json += std::to_string(rid);
            json += "\": {\"name\": \"";
            detail::json_escape(info.name.c_str(), json);
            json += "\", \"file\": \"";
            detail::json_escape(info.file.c_str(), json);
            json += "\", \"line\": ";
            json += std::to_string(info.line);
            json += "}";
        }
    }
    if (!probe_registry().empty()) json += "\n  ";
    json += "},\n";

    // Build tree from roots (parent_id == 0)
    std::vector<detail::TreeNode> roots;
    {
        auto rit = children_map.find(0u);
        if (rit != children_map.end()) {
            for (uint32_t root_id : rit->second) {
                auto sit = stats.find(root_id);
                if (sit == stats.end()) continue;
                const auto& rs = sit->second;
                detail::TreeNode root;
                root.probe_id = rs.probe_id;
                root.calls    = rs.calls;
                root.total_ns = rs.total_ns;
                root.self_ns  = rs.self_ns;
                root.pct      = rs.pct_of_parent;
                detail::build_tree(root, stats, children_map);
                roots.push_back(std::move(root));
            }
        }
    }

    json += "  \"results\": [\n";
    for (size_t i = 0; i < roots.size(); ++i) {
        detail::emit_tree_json(roots[i], json, 2);
        if (i + 1 < roots.size()) json += ",";
        json += "\n";
    }
    json += "  ]\n";
    json += "}\n";

    std::fputs(json.c_str(), stdout);
    std::fflush(stdout);
}

} // namespace profiler

#endif // CPP_PERF_PROBE_H
