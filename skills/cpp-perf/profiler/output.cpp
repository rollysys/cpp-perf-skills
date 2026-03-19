#include "common.h"

#include <cstdio>
#include <cstring>
#include <ctime>
#include <fstream>
#include <sstream>
#include <string>
#include <map>
#include <vector>

#ifdef __APPLE__
#include <sys/sysctl.h>
#endif

namespace profiler {

// ============================================================
// CPU model detection
// ============================================================

struct CpuInfo {
    std::string name;
    std::string arch;
    std::string vendor;

    // Optional — populated from lookup table for known CPUs
    int issue_width    = 0;
    int dispatch_width = 0;
    int reorder_buffer = 0;
    int alu_units      = 0;
    int fp_units       = 0;
    int load_units     = 0;
    int store_units    = 0;
    int branch_units   = 0;
    int gpr            = 0;
    int neon_regs      = 0;  // or xmm/ymm count for x86
};

// Map ARM (implementer, part) pairs to known CPU names
#if defined(__linux__) && defined(__aarch64__)
struct ArmPartEntry {
    unsigned implementer;
    unsigned part;
    const char* name;
    const char* vendor;
};

static const ArmPartEntry arm_known_parts[] = {
    // ARM Ltd (0x41)
    { 0x41, 0xd03, "Cortex-A53",    "ARM" },
    { 0x41, 0xd04, "Cortex-A35",    "ARM" },
    { 0x41, 0xd05, "Cortex-A55",    "ARM" },
    { 0x41, 0xd07, "Cortex-A57",    "ARM" },
    { 0x41, 0xd08, "Cortex-A72",    "ARM" },
    { 0x41, 0xd09, "Cortex-A73",    "ARM" },
    { 0x41, 0xd0a, "Cortex-A75",    "ARM" },
    { 0x41, 0xd0b, "Cortex-A76",    "ARM" },
    { 0x41, 0xd0c, "Neoverse-N1",   "ARM" },
    { 0x41, 0xd0d, "Cortex-A77",    "ARM" },
    { 0x41, 0xd40, "Neoverse-V1",   "ARM" },
    { 0x41, 0xd41, "Cortex-A78",    "ARM" },
    { 0x41, 0xd44, "Cortex-X1",     "ARM" },
    { 0x41, 0xd46, "Cortex-A510",   "ARM" },
    { 0x41, 0xd47, "Cortex-A710",   "ARM" },
    { 0x41, 0xd48, "Cortex-X2",     "ARM" },
    { 0x41, 0xd49, "Neoverse-N2",   "ARM" },
    { 0x41, 0xd4a, "Neoverse-E1",   "ARM" },
    // Apple (0x61)
    { 0x61, 0x022, "Apple M1 Icestorm",  "Apple" },
    { 0x61, 0x023, "Apple M1 Firestorm", "Apple" },
    { 0x61, 0x024, "Apple M1 Pro",       "Apple" },
    { 0x61, 0x025, "Apple M1 Pro",       "Apple" },
    { 0x61, 0x028, "Apple M1 Max",       "Apple" },
    { 0x61, 0x032, "Apple M2 Blizzard",  "Apple" },
    { 0x61, 0x033, "Apple M2 Avalanche", "Apple" },
    // Qualcomm (0x51)
    { 0x51, 0x802, "Kryo 385 Gold",   "Qualcomm" },
    { 0x51, 0x803, "Kryo 385 Silver", "Qualcomm" },
    { 0x51, 0x804, "Kryo 485 Gold",   "Qualcomm" },
    { 0x51, 0x805, "Kryo 485 Silver", "Qualcomm" },
    { 0, 0, nullptr, nullptr }
};
#endif

// Pipeline/register lookup for a few well-known cores
struct KnownCoreMicroarch {
    const char* name;
    int issue_width, dispatch_width, reorder_buffer;
    int alu, fp, load, store, branch;
    int gpr, neon_regs;
};

static const KnownCoreMicroarch known_microarch[] = {
    { "Cortex-A78",  4, 2, 160,  3, 2, 2, 1, 1,  31, 32 },
    { "Cortex-A55",  2, 2,   0,  1, 1, 1, 1, 1,  31, 32 },
    { "Cortex-A76",  4, 2, 128,  3, 2, 2, 1, 1,  31, 32 },
    { "Neoverse-N1", 4, 2, 128,  3, 2, 2, 1, 1,  31, 32 },
    { "Neoverse-N2", 5, 2, 160,  3, 2, 2, 2, 1,  31, 32 },
    { nullptr, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0 }
};

static void fill_microarch(CpuInfo& info) {
    for (int i = 0; known_microarch[i].name; ++i) {
        if (info.name.find(known_microarch[i].name) != std::string::npos) {
            auto& m = known_microarch[i];
            info.issue_width    = m.issue_width;
            info.dispatch_width = m.dispatch_width;
            info.reorder_buffer = m.reorder_buffer;
            info.alu_units      = m.alu;
            info.fp_units       = m.fp;
            info.load_units     = m.load;
            info.store_units    = m.store;
            info.branch_units   = m.branch;
            info.gpr            = m.gpr;
            info.neon_regs      = m.neon_regs;
            return;
        }
    }
}

static CpuInfo detect_cpu() {
    CpuInfo info;

    // --- Architecture ---
#if defined(__aarch64__)
    info.arch = "aarch64";
#elif defined(__x86_64__)
    info.arch = "x86_64";
#else
    info.arch = "unknown";
#endif

    // --- Platform-specific detection ---

#if defined(__APPLE__)
    // macOS: use sysctl for both ARM and x86 Macs
    {
        char brand[256] = {};
        size_t len = sizeof(brand);
        if (sysctlbyname("machdep.cpu.brand_string", brand, &len, nullptr, 0) == 0) {
            info.name = brand;
        } else {
            // Fallback for Apple Silicon where brand_string may not exist
            len = sizeof(brand);
            if (sysctlbyname("machdep.cpu.brand", brand, &len, nullptr, 0) == 0) {
                info.name = brand;
            }
        }

        // Vendor
#if defined(__aarch64__)
        info.vendor = "Apple";
        if (info.name.empty()) {
            // Try to get chip name from hw.chip
            len = sizeof(brand);
            if (sysctlbyname("machdep.cpu.chip", brand, &len, nullptr, 0) == 0) {
                info.name = brand;
            } else {
                info.name = "Apple Silicon (unknown)";
            }
        }
#else
        // x86 Mac — parse vendor from brand string
        if (info.name.find("Intel") != std::string::npos)
            info.vendor = "Intel";
        else if (info.name.find("AMD") != std::string::npos)
            info.vendor = "AMD";
        else
            info.vendor = "unknown";
#endif
    }

#elif defined(__linux__)
    // Linux: parse /proc/cpuinfo
    {
        std::ifstream cpuinfo("/proc/cpuinfo");
        if (cpuinfo.is_open()) {
            std::string line;
            unsigned implementer = 0;
            unsigned part = 0;
            bool found_impl = false, found_part = false;

            while (std::getline(cpuinfo, line)) {
#if defined(__aarch64__)
                // aarch64 fields
                if (line.find("CPU implementer") != std::string::npos) {
                    auto pos = line.find(':');
                    if (pos != std::string::npos) {
                        implementer = (unsigned)strtoul(line.c_str() + pos + 1, nullptr, 0);
                        found_impl = true;
                    }
                } else if (line.find("CPU part") != std::string::npos) {
                    auto pos = line.find(':');
                    if (pos != std::string::npos) {
                        part = (unsigned)strtoul(line.c_str() + pos + 1, nullptr, 0);
                        found_part = true;
                    }
                }
                // Stop after finding both fields from the first core
                if (found_impl && found_part) break;
#else
                // x86_64 fields
                if (line.find("model name") != std::string::npos) {
                    auto pos = line.find(':');
                    if (pos != std::string::npos) {
                        info.name = line.substr(pos + 2);
                    }
                    break;
                }
#endif
            }

#if defined(__aarch64__)
            if (found_impl && found_part) {
                // Look up in known parts table
                bool found = false;
                for (int i = 0; arm_known_parts[i].name; ++i) {
                    if (arm_known_parts[i].implementer == implementer &&
                        arm_known_parts[i].part == part) {
                        info.name = arm_known_parts[i].name;
                        info.vendor = arm_known_parts[i].vendor;
                        found = true;
                        break;
                    }
                }
                if (!found) {
                    char buf[64];
                    snprintf(buf, sizeof(buf), "ARM-impl-0x%02x-part-0x%03x",
                             implementer, part);
                    info.name = buf;
                    info.vendor = "ARM";
                }
            } else {
                info.name = "aarch64 (unknown)";
                info.vendor = "ARM";
            }
#else
            // x86 vendor detection from name
            if (info.name.find("Intel") != std::string::npos)
                info.vendor = "Intel";
            else if (info.name.find("AMD") != std::string::npos)
                info.vendor = "AMD";
            else
                info.vendor = "unknown";
#endif
        } else {
            info.name = "unknown";
            info.vendor = "unknown";
        }
    }

#else
    // Unsupported platform
    info.name = "unknown";
    info.vendor = "unknown";
#endif

    // Fill in microarch details if we recognize the CPU
    fill_microarch(info);

    return info;
}

// ============================================================
// YAML formatting helpers
// ============================================================

// Format a value: integers as int, otherwise 1 decimal place
static std::string fmt_val(double v) {
    char buf[64];
    if (v == (int64_t)v && v >= -1e15 && v <= 1e15) {
        snprintf(buf, sizeof(buf), "%lld", (long long)v);
    } else {
        snprintf(buf, sizeof(buf), "%.2f", v);
        // Strip trailing zeros after decimal (but keep at least one)
        char* dot = strchr(buf, '.');
        if (dot) {
            char* end = buf + strlen(buf) - 1;
            while (end > dot + 1 && *end == '0') {
                *end = '\0';
                --end;
            }
        }
    }
    return buf;
}

// ============================================================
// Structured YAML output
// ============================================================

// Split "a.b" into {"a", "b"}, or "a" into {"a"}
static std::vector<std::string> split_dot(const std::string& s) {
    std::vector<std::string> parts;
    std::istringstream iss(s);
    std::string token;
    while (std::getline(iss, token, '.')) {
        parts.push_back(token);
    }
    return parts;
}

void output_yaml() {
    auto& r = results();

    // --- Detect CPU ---
    CpuInfo cpu = detect_cpu();

    // --- Header ---
    time_t now = time(nullptr);
    struct tm tm_buf;
    localtime_r(&now, &tm_buf);
    char date_str[32];
    strftime(date_str, sizeof(date_str), "%Y-%m-%d", &tm_buf);

    printf("# Auto-generated by cpp-perf profiler\n");
    printf("# Date: %s\n", date_str);
    printf("name: %s\n", cpu.name.c_str());
    printf("arch: %s\n", cpu.arch.c_str());
    printf("vendor: %s\n", cpu.vendor.c_str());

    // --- Pipeline info (if known) ---
    if (cpu.issue_width > 0) {
        printf("\npipeline:\n");
        printf("  issue_width: %d\n", cpu.issue_width);
        printf("  dispatch_width: %d\n", cpu.dispatch_width);
        if (cpu.reorder_buffer > 0) {
            printf("  reorder_buffer: %d\n", cpu.reorder_buffer);
        }
        printf("  functional_units:\n");
        printf("    alu: %d\n", cpu.alu_units);
        printf("    fp: %d\n", cpu.fp_units);
        printf("    load: %d\n", cpu.load_units);
        printf("    store: %d\n", cpu.store_units);
        printf("    branch: %d\n", cpu.branch_units);
    }

    // --- Registers (if known) ---
    if (cpu.gpr > 0) {
        printf("\nregisters:\n");
        printf("  gpr: %d\n", cpu.gpr);
        if (cpu.neon_regs > 0) {
            printf("  neon: %d\n", cpu.neon_regs);
        }
    }

    // ================================================================
    // Emit measured data in schema-compliant structured YAML
    // ================================================================

    // ---- cache section ----
    // Emit cache hierarchy (l1d, l2, l3) as flow mappings
    bool has_cache = (r.count("cache.l1d") || r.count("cache.l2") || r.count("cache.l3"));
    if (has_cache) {
        printf("\ncache:\n");

        auto emit_cache_level = [&](const char* section, const char* label) {
            auto it = r.find(section);
            if (it == r.end()) return;
            auto& kvs = it->second;

            printf("  %s: {", label);
            bool first = true;
            // Emit in a canonical order: size_kb, line_bytes, latency
            const char* order[] = { "size_kb", "line_bytes", "latency" };
            for (auto key : order) {
                auto vit = kvs.find(key);
                if (vit != kvs.end()) {
                    if (!first) printf(",");
                    printf(" %s: %s", key, fmt_val(vit->second).c_str());
                    first = false;
                }
            }
            // Any remaining keys not in the canonical order
            for (auto& [k, v] : kvs) {
                bool in_order = false;
                for (auto o : order) { if (k == o) { in_order = true; break; } }
                if (!in_order) {
                    if (!first) printf(",");
                    printf(" %s: %s", k.c_str(), fmt_val(v).c_str());
                    first = false;
                }
            }
            printf(" }\n");
        };

        emit_cache_level("cache.l1d", "l1d");
        emit_cache_level("cache.l2",  "l2");
        emit_cache_level("cache.l3",  "l3");
    }

    // ---- instructions section ----
    // Transform keys like add_lat/add_tp into: add: { lat: X, tp: Y }
    {
        // Collect all instruction subsections
        struct InstrGroup {
            std::string subsection; // e.g. "integer", "fp", "simd"
            // operation name -> { "lat": val, "tp": val } or standalone keys
            std::map<std::string, std::map<std::string, double>> ops;
            std::map<std::string, double> standalone;
        };

        std::vector<InstrGroup> groups;
        // Ordered list of subsection names we expect
        const char* subsections[] = { "integer", "fp", "fp32", "fp64", "simd", "simd_4xf32", "neon", "memory" };

        for (auto sub : subsections) {
            std::string section = std::string("instructions.") + sub;
            auto it = r.find(section);
            if (it == r.end()) continue;

            InstrGroup g;
            g.subsection = sub;

            for (auto& [key, val] : it->second) {
                // Check if key ends with _lat or _tp
                if (key.size() > 4 && key.substr(key.size() - 4) == "_lat") {
                    std::string op = key.substr(0, key.size() - 4);
                    g.ops[op]["lat"] = val;
                } else if (key.size() > 3 && key.substr(key.size() - 3) == "_tp") {
                    std::string op = key.substr(0, key.size() - 3);
                    g.ops[op]["tp"] = val;
                } else {
                    g.standalone[key] = val;
                }
            }

            groups.push_back(std::move(g));
        }

        if (!groups.empty()) {
            printf("\ninstructions:\n");
            for (auto& g : groups) {
                printf("  %s:\n", g.subsection.c_str());

                for (auto& [op, attrs] : g.ops) {
                    printf("    %s: {", op.c_str());
                    bool first = true;
                    // Emit lat first, then tp
                    auto lat_it = attrs.find("lat");
                    if (lat_it != attrs.end()) {
                        printf(" lat: %s", fmt_val(lat_it->second).c_str());
                        first = false;
                    }
                    auto tp_it = attrs.find("tp");
                    if (tp_it != attrs.end()) {
                        if (!first) printf(",");
                        printf(" tp: %s", fmt_val(tp_it->second).c_str());
                        first = false;
                    }
                    printf(" }\n");
                }

                for (auto& [key, val] : g.standalone) {
                    printf("    %s: %s\n", key.c_str(), fmt_val(val).c_str());
                }
            }
        }
    }

    // ---- branch section ----
    if (r.count("branch")) {
        printf("\nbranch:\n");
        auto& kvs = r["branch"];
        // mispredict_penalty first
        auto mp_it = kvs.find("mispredict_penalty");
        if (mp_it != kvs.end()) {
            printf("  mispredict_penalty: %s\n", fmt_val(mp_it->second).c_str());
        }
        // remaining keys
        for (auto& [k, v] : kvs) {
            if (k == "mispredict_penalty") continue;
            printf("  %s: %s\n", k.c_str(), fmt_val(v).c_str());
        }
    }

    // ---- memory_system section ----
    if (r.count("memory_system")) {
        printf("\nmemory_system:\n");
        auto& kvs = r["memory_system"];
        for (auto& [k, v] : kvs) {
            printf("  %s: %s\n", k.c_str(), fmt_val(v).c_str());
        }
    }

    // ---- os_overhead section ----
    if (r.count("os_overhead")) {
        printf("\nos_overhead:\n");
        auto& kvs = r["os_overhead"];
        for (auto& [k, v] : kvs) {
            printf("  %s: %s\n", k.c_str(), fmt_val(v).c_str());
        }
    }

    // ---- cache.latency_curve — raw data as comment block ----
    if (r.count("cache.latency_curve")) {
        printf("\n# --- Raw cache latency curve (cycles per access) ---\n");
        printf("# cache_latency_curve:\n");
        auto& kvs = r["cache.latency_curve"];
        for (auto& [k, v] : kvs) {
            printf("#   %s: %s\n", k.c_str(), fmt_val(v).c_str());
        }
    }

    // ---- Any remaining sections not handled above ----
    {
        // Sections we already emitted
        auto is_handled = [](const std::string& section) -> bool {
            if (section == "branch" || section == "memory_system" || section == "os_overhead")
                return true;
            if (section.rfind("cache.", 0) == 0)
                return true;
            if (section.rfind("instructions.", 0) == 0)
                return true;
            return false;
        };

        for (auto& [section, kvs] : r) {
            if (is_handled(section)) continue;

            // Generic nested output
            auto parts = split_dot(section);
            if (parts.size() == 1) {
                printf("\n%s:\n", parts[0].c_str());
                for (auto& [k, v] : kvs) {
                    printf("  %s: %s\n", k.c_str(), fmt_val(v).c_str());
                }
            } else {
                // Multi-level nesting
                printf("\n%s:\n", parts[0].c_str());
                // indent for remaining levels
                for (size_t i = 1; i < parts.size(); ++i) {
                    std::string indent(2 * i, ' ');
                    printf("%s%s:\n", indent.c_str(), parts[i].c_str());
                }
                std::string indent(2 * parts.size(), ' ');
                for (auto& [k, v] : kvs) {
                    printf("%s%s: %s\n", indent.c_str(), k.c_str(), fmt_val(v).c_str());
                }
            }
        }
    }
}

} // namespace profiler
