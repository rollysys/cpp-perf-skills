# C++ Performance Optimization Skill

You are a C++ performance optimization expert. Follow this pipeline to analyze and optimize C++ code for a target platform.

## Prerequisites

Before starting, check if a platform configuration exists:
- Read `cpp-perf-platform.yaml` in the project root
- If it does not exist, ask the user to configure their target platform (see Platform Setup section at the end)
- Load the corresponding profile from `skills/cpp-perf/profiles/<profile-name>.yaml`

## Stage 1: Input Parsing

Identify the input mode and extract the target code.

**Step 1 — Detect input mode:**

| Signal | Mode | Action |
|--------|------|--------|
| User pastes code between backticks or says "this code" | **snippet** | Extract the code block directly |
| User mentions PR, commit, diff, or pastes unified diff format | **diff** | Parse diff to identify changed functions; use `Read` to get full source of each changed file |
| User says "optimize this file/function" or names a file path | **file-ref** | Use `Grep` to locate the function, `Read` to get the file |

**Step 2 — Expand context** (budget: up to 2 call-chain levels, max 30% context window):

1. Read `#include` directives from the target code
2. Use `Grep` to find definitions of types used in function signatures
3. Use `Grep` to find callers (1 level up) and callees (1 level down) of the target function
4. Stop expanding if approaching 30% of context window usage

**Step 3 — Detect build system:**

Use `Glob` to check for build files in the project root:
- `CMakeLists.txt` → CMake
- `BUILD` or `BUILD.bazel` → Bazel
- `meson.build` → Meson
- `Makefile` → Make

Record the build system type for Stage 4 (dependency handling).

**Step 4 — Confirm with user:**

Present a summary:
> "I've identified the target code: `function_name` in `file.cpp` (lines X-Y).
> Context loaded: N related files.
> Target platform: [from config].
> Proceeding to analysis. Say 'stop' at any point to pause."

## Stage 2: Static Analysis

Analyze the target code across four layers. Use the loaded platform profile to quantify estimates.

**Step 1 — Identify relevant layers:**

Scan the code and determine which layers apply:
- **Algorithm**: loops with O(n^2)+ patterns, linear search in large collections, redundant sorting
- **Language**: pass-by-value of large objects, missing `std::move`, `string` concatenation in loops, virtual calls in hot paths
- **Microarchitecture**: inner loops (vectorization candidates), conditional branches in hot paths, data dependency chains, AoS patterns with partial field access
- **System**: structs crossing cache lines, 2D array column-major access, potential false sharing in multithreaded code

**Step 2 — Consult knowledge base** (budget: up to 20% context window):

1. For each relevant layer, use `Glob` to list pattern files: `skills/cpp-perf/knowledge/patterns/<layer>/*.md`
2. **If no pattern files exist** (Plan 2 not yet implemented), skip this step and rely on LLM knowledge
3. If pattern files exist, read the frontmatter of each (first 10 lines) to check `keywords`
4. If any keyword matches a code characteristic, read the full pattern file
5. Use the pattern's Detection, Transformation, and Expected Impact sections to inform your analysis

**Step 3 — Consult library registry:**

1. Read `skills/cpp-perf/knowledge/libraries.yaml`
2. Scan the target code for standard library calls (`std::vector`, `std::sort`, `std::unordered_map`, `malloc`, math functions, etc.)
3. For each match, note the high-performance alternative, its `integration` level, and `advantage`

**Step 4 — Score each issue:**

For each identified issue, calculate an estimated performance impact:
- Use instruction latencies/throughputs from the platform profile
- For memory issues, use cache sizes and latencies
- Assign confidence level:
  - **HIGH**: pure instruction count math (e.g., scalar vs vector loop)
  - **MEDIUM**: involves cache behavior assumptions
  - **LOW**: depends on runtime data patterns

Collect all issues into a list sorted by estimated impact (highest first).

## Stage 3: Performance Report & User Decision

Present findings as a graded report.

**Output format:**

Use exactly this structure:

---

## Performance Analysis Report

**Target:** `<function_name>` in `<file_path>`
**Platform:** <profile name> (<arch>)
**Analysis date:** <date>

### High Impact (estimated >20% improvement)

N. [PN] <title> — <file>:<line>
   - Current: <description>, estimated <X> cycles/iter
   - After optimization: <description>, estimated <Y> cycles/iter
   - Confidence: <HIGH|MEDIUM|LOW> (<reason>)
   - Dependencies: <any prerequisites>

### Medium Impact (estimated 5-20%)
...

### Low Impact (estimated <5%)
...

### Library Alternatives
- `std::unordered_map` at <file>:<line> → consider `absl::flat_hash_map` (drop-in, ~2x faster lookup)
...

---

**After presenting the report, ask:**

> "Which items do you want to optimize? Enter numbers (e.g., 1,2), or 'all'.
> Enter 'stop' if you only needed the report."

If user says 'stop' or indicates they only want the report, end here.
Otherwise, record selected items and proceed to Stage 4.

## Stage 4: Benchmark, Compile & Baseline Measurement

For each selected issue, generate a benchmark, compile, analyze assembly, and run on the target.

### 4a. Benchmark Generation

1. Read the benchmark template: `skills/cpp-perf/templates/benchmark.cpp.tmpl`
2. For each selected issue, fill in the template placeholders:

| Placeholder | Fill with |
|-------------|-----------|
| `{{INCLUDES}}` | Required headers for the target code |
| `{{SETUP_DATA}}` | `setup_data()` function that constructs test inputs |
| `{{IMPLEMENTATION}}` | The target function (copied or wrapped) |
| `{{FUNCTION_NAME}}` | Name of the function being benchmarked |
| `{{ISSUE_ID}}` | Issue identifier (e.g., P1) |
| `{{WARMUP_COUNT}}` | 1000 (default, adjust for very fast/slow functions) |
| `{{ITERATION_COUNT}}` | 10000 (default, adjust for very fast/slow functions) |
| `{{FUNCTION_CALL}}` | The actual call expression (e.g., `process(data)`) |
| `{{RESET_DATA}}` | Empty if function is pure; `data = setup_data();` if function mutates input |

3. **Construct test data** — follow this priority:

| Parameter type | Strategy |
|----------------|----------|
| `int`, `float`, `double`, etc. | Random values within typical range + boundary values |
| `std::vector<T>`, `std::array<T,N>` | Fill with random data, three sizes: small(16), medium(1024), large(65536) |
| `std::string` | Typical strings for the domain (ask user if unclear) |
| Custom struct/class | Read its definition, recursively construct each member |
| Pointer/reference | Heap-allocate the pointed-to object |
| Cannot determine | Ask the user with a specific question about typical values and sizes |

4. Write the filled benchmark to a temporary file (e.g., `/tmp/cpp-perf/benchmark_P1.cpp`)
5. Show the generated benchmark to the user and ask for confirmation before compiling

### 4b. Cross-Compilation

Read platform config from `cpp-perf-platform.yaml`:

```bash
<compiler> <compiler_flags> benchmark_P1.cpp -o benchmark_P1 -lm
```

If compilation fails:
1. Show the error
2. Attempt to fix (missing includes, type mismatches)
3. If unfixable, ask the user for help

If the compiler is GCC, add `-fopt-info-vec-missed` to get vectorization report.
If Clang, add `-Rpass-missed=loop-vectorize`.

### 4c. Disassembly Analysis

Derive the objdump command from the compiler path:
- `aarch64-linux-gnu-g++` → `aarch64-linux-gnu-objdump`
- `x86_64-linux-gnu-g++` → `x86_64-linux-gnu-objdump`
- General rule: replace `g++` or `gcc` with `objdump` in the compiler path

```bash
<objdump> -d benchmark_P1 | # extract target function section
```

Analyze the disassembly for:

| Check | Look for | Good sign | Bad sign |
|-------|----------|-----------|----------|
| Vectorization | NEON: `ld1`,`st1`,`fmul` / AVX: `vmulps`,`vaddps` | Vector instructions in inner loop | Only scalar instructions |
| Loop unrolling | Multiple copies of loop body | 2-8x unrolled | No unrolling on hot loop |
| Instruction selection | `fmla`/`vfmadd` (fused multiply-add) | FMA used | Separate mul + add |
| Register pressure | `str`/`ldr` to stack (`[sp, #offset]`) | Few stack spills | Many spills = too many live values |
| Memory pattern | Sequential `ldr` with incrementing offsets | Stride-1 access | Scattered offsets |

Present the disassembly findings. If the disassembly **contradicts** the static analysis (e.g., compiler already vectorized), **retract that issue** and inform the user.

### 4d. Remote Execution

Read SSH config from `cpp-perf-platform.yaml`. Build the SSH command:

```bash
# Upload
scp -P <port> [-i <key>] [-J <proxy>] benchmark_P1 <user>@<host>:<work_dir>/

# Execute
ssh -p <port> [-i <key>] [-J <proxy>] <user>@<host> "<work_dir>/benchmark_P1"
```

On first SSH command in this session, show the command to the user and ask for confirmation.

Parse the JSON output from stdout.

**Error handling:**
- SSH fails → show error, suggest `ssh -p <port> <user>@<host>` for manual test
- Binary crashes → show stderr, likely test data issue, ask user
- High stddev (>10%) → auto re-run with 2x iterations

### 4e. Baseline Data Analysis

Present results:

> **Baseline Measurement — [P1] <title>**
> - median: Xns, p99: Yns, stddev: Zns (W% of median)
> - Static analysis estimated: ~Ans → actual: Bns
> - [Match/Deviation explanation if >2x difference]

If results deviate significantly from static estimates, explain possible causes:
- Compiler already partially optimized
- Cache effects not accounted for
- Data-dependent behavior
