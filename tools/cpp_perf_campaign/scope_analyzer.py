"""AST-based scope analyzer for C++ code fragments.

Uses tree-sitter to parse code (no compiler, no headers required).
Works on full files, functions, or raw code snippets.

Produces a scope profile that can be injected into the optimization
prompt so the agent has objective structural data before starting.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tree_sitter_cpp as tscpp
from tree_sitter import Language, Parser

CPP_LANGUAGE = Language(tscpp.language())

LOOP_TYPES = {"for_statement", "while_statement", "do_statement", "for_range_loop"}
BRANCH_TYPES = {"if_statement", "switch_statement", "conditional_expression"}
CALL_TYPES = {"call_expression"}
FUNC_DEF_TYPES = {"function_definition"}
COMPOUND_STMT = {"compound_statement"}


@dataclass
class ScopeProfile:
    # Basic metrics
    total_lines: int = 0
    statement_count: int = 0

    # Loop analysis
    loop_count: int = 0
    max_loop_depth: int = 0
    innermost_loop_statements: int = 0
    has_nested_loops: bool = False

    # Branch analysis
    branch_count: int = 0
    branches_in_loops: int = 0

    # Call analysis
    call_count: int = 0
    calls_in_loops: int = 0
    distinct_callees: list[str] = field(default_factory=list)

    # Structural hints
    has_pointer_arithmetic: bool = False
    has_array_subscript: bool = False
    has_float_literal: bool = False
    has_simd_hint: bool = False  # __restrict__, pragma omp simd, etc.
    function_count: int = 0

    # Derived classification
    code_type: str = "unknown"  # expression | loop | function_call | mixed
    scope_depth: str = "shallow"  # shallow | medium | deep

    # Optimization budget (set by classify)
    max_strategies: int = 1
    recommended_strategies: list[str] = field(default_factory=list)
    skip_reason: str | None = None

    def classify(self) -> None:
        """Classify code and assign a conservative optimization budget.

        Conservative means:
        - We never skip something that *might* benefit from optimization.
        - We limit effort on things that almost certainly won't benefit.
        - We scale strategies to structural complexity, not to ambition.
        """
        # --- code_type ---
        if self.loop_count == 0 and self.call_count == 0:
            self.code_type = "expression"
        elif self.loop_count > 0 and self.calls_in_loops == 0:
            self.code_type = "loop"
        elif self.loop_count == 0 and self.call_count > 0:
            self.code_type = "function_call"
        else:
            self.code_type = "mixed"

        # --- scope_depth (unchanged formula) ---
        score = (
            self.statement_count
            + self.loop_count * 5
            + self.max_loop_depth * 10
            + self.branch_count * 2
            + self.call_count
        )
        if score <= 10:
            self.scope_depth = "shallow"
        elif score <= 40:
            self.scope_depth = "medium"
        else:
            self.scope_depth = "deep"

        # --- Conservative strategy assignment ---
        #
        # Principle: only recommend strategies that have structural evidence
        # in the AST.  Never recommend a strategy just because it exists.
        # If nothing matches, still allow one generic pass so we don't
        # silently skip a target that might surprise us.

        strategies: list[str] = []

        # Expression-only code: very limited optimization surface.
        if self.code_type == "expression":
            if self.statement_count <= 3:
                self.skip_reason = "expression_too_small"
                self.max_strategies = 0
                self.recommended_strategies = []
                return
            strategies.append("strength_reduction")
            self.max_strategies = 1
            self.recommended_strategies = strategies
            return

        # Function-call-only code: optimization is mostly about inlining
        # and call overhead, not compute/memory patterns.
        if self.code_type == "function_call" and self.loop_count == 0:
            strategies.append("inlining")
            if self.call_count >= 3:
                strategies.append("call_overhead")
            self.max_strategies = min(len(strategies), 2)
            self.recommended_strategies = strategies
            return

        # --- Loop-bearing code: main optimization surface ---

        # Always: dependency chain analysis (applies to any loop)
        strategies.append("dependency_chain")

        # Nested loops → memory access pattern is likely the bottleneck
        if self.has_nested_loops:
            strategies.append("loop_interchange")
            strategies.append("loop_tiling")

        # Branches inside loops → branchless / cmov opportunity
        if self.branches_in_loops > 0:
            strategies.append("branchless")

        # Array/pointer access in loops → vectorization candidate
        if self.has_array_subscript or self.has_pointer_arithmetic:
            strategies.append("vectorize")
            # Multi-statement innermost loop → unroll may help
            if self.innermost_loop_statements >= 3:
                strategies.append("unroll")

        # Floating point → FP-specific patterns
        if self.has_float_literal or self._likely_fp():
            strategies.append("multi_accumulator")

        # Deep scope → prefetch is worth trying
        if self.scope_depth == "deep" or self.has_nested_loops:
            strategies.append("prefetch")

        # Calls inside loops → inline or hoist opportunity
        if self.calls_in_loops > 0:
            strategies.append("hoist_calls")

        # Already has SIMD hints → check if they're effective
        if self.has_simd_hint:
            strategies.append("verify_simd")

        # Budget: shallow=2, medium=4, deep=all
        if self.scope_depth == "shallow":
            self.max_strategies = min(len(strategies), 2)
        elif self.scope_depth == "medium":
            self.max_strategies = min(len(strategies), 4)
        else:
            self.max_strategies = len(strategies)

        self.recommended_strategies = strategies[:self.max_strategies]

    def _likely_fp(self) -> bool:
        """Heuristic: code probably operates on floating point."""
        # If we saw float literals, yes.  Also true if callee names
        # suggest math (sqrt, sin, fma, etc.) — but we keep it simple.
        return self.has_float_literal


def _count_statements(node) -> int:
    """Count direct statement children (not recursing into nested blocks)."""
    count = 0
    for child in node.children:
        if child.type.endswith("_statement") or child.type in (
            "expression_statement", "return_statement", "declaration",
        ):
            count += 1
    return count


def _walk(node, loop_depth: int, profile: ScopeProfile) -> None:
    """Recursive AST walk collecting metrics."""
    t = node.type

    if t in FUNC_DEF_TYPES:
        profile.function_count += 1

    if t in LOOP_TYPES:
        profile.loop_count += 1
        new_depth = loop_depth + 1
        if new_depth > profile.max_loop_depth:
            profile.max_loop_depth = new_depth
        if new_depth >= 2:
            profile.has_nested_loops = True
    else:
        new_depth = loop_depth

    if t.endswith("_statement") or t == "declaration":
        profile.statement_count += 1

    if t in BRANCH_TYPES:
        profile.branch_count += 1
        if loop_depth > 0:
            profile.branches_in_loops += 1

    if t in CALL_TYPES:
        profile.call_count += 1
        if loop_depth > 0:
            profile.calls_in_loops += 1
        # Extract callee name
        if node.children:
            callee = node.children[0]
            name = callee.text.decode("utf-8", errors="replace") if callee.text else ""
            if name and name not in profile.distinct_callees:
                profile.distinct_callees.append(name)

    if t == "subscript_expression":
        profile.has_array_subscript = True

    if t == "pointer_expression" or t == "pointer_declarator":
        profile.has_pointer_arithmetic = True

    if t in ("number_literal", "float_literal"):
        text = (node.text or b"").decode("utf-8", errors="replace")
        if "." in text or "f" in text.lower() or "e" in text.lower():
            profile.has_float_literal = True

    if t == "type_qualifier":
        text = (node.text or b"").decode("utf-8", errors="replace")
        if "__restrict" in text or "restrict" in text:
            profile.has_simd_hint = True

    if t == "preproc_call":
        text = (node.text or b"").decode("utf-8", errors="replace")
        if "pragma" in text.lower() and ("simd" in text.lower() or "ivdep" in text.lower()):
            profile.has_simd_hint = True

    # Count innermost loop body statements
    if t in LOOP_TYPES:
        has_inner_loop = False
        for child in node.children:
            if child.type in LOOP_TYPES:
                has_inner_loop = True
                break
            if child.type in COMPOUND_STMT:
                for gc in child.children:
                    if gc.type in LOOP_TYPES:
                        has_inner_loop = True
                        break
        if not has_inner_loop:
            # This is an innermost loop
            for child in node.children:
                if child.type in COMPOUND_STMT:
                    stmts = _count_statements(child)
                    if stmts > profile.innermost_loop_statements:
                        profile.innermost_loop_statements = stmts

    for child in node.children:
        _walk(child, new_depth, profile)


@dataclass
class FunctionTarget:
    """A single function extracted from a source file."""
    name: str
    start_line: int  # 1-based
    end_line: int  # 1-based, inclusive
    source: str  # the raw text of the function
    profile: ScopeProfile
    file_path: str = ""

    @property
    def line_range(self) -> str:
        return f"{self.start_line}-{self.end_line}"


def _extract_function_name(node) -> str:
    """Extract the function name from a function_definition node."""
    declarator = None
    for child in node.children:
        if child.type in ("function_declarator", "reference_declarator",
                          "pointer_declarator", "qualified_identifier"):
            declarator = child
            break
        if child.type == "declarator":
            declarator = child
            break
    if declarator is None:
        # Walk one more level for nested declarators
        for child in node.children:
            for gc in child.children:
                if gc.type in ("function_declarator", "qualified_identifier"):
                    declarator = gc
                    break
            if declarator:
                break

    if declarator is None:
        return "<unknown>"

    # Find the identifier inside the declarator chain
    queue = [declarator]
    while queue:
        n = queue.pop(0)
        if n.type in ("identifier", "field_identifier", "destructor_name"):
            return n.text.decode("utf-8", errors="replace")
        if n.type == "qualified_identifier":
            # Return the full qualified name
            return n.text.decode("utf-8", errors="replace")
        if n.type == "template_function":
            return n.text.decode("utf-8", errors="replace")
        queue.extend(n.children)
    return declarator.text.decode("utf-8", errors="replace").split("(")[0].strip()


def _find_functions(root_node, source_bytes: bytes) -> list[tuple[str, int, int, str]]:
    """Find all function definitions and return (name, start_line, end_line, source)."""
    functions = []
    for child in root_node.children:
        if child.type == "function_definition":
            name = _extract_function_name(child)
            start = child.start_point[0] + 1  # 1-based
            end = child.end_point[0] + 1
            text = source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
            functions.append((name, start, end, text))
        # Also handle functions inside namespaces and classes
        elif child.type in ("namespace_definition", "class_specifier",
                            "struct_specifier", "template_declaration"):
            # Recurse into compound body
            for gc in child.children:
                if gc.type in ("declaration_list", "field_declaration_list",
                               "compound_statement"):
                    inner = _find_functions(gc, source_bytes)
                    functions.extend(inner)
                elif gc.type == "function_definition":
                    name = _extract_function_name(gc)
                    start = gc.start_point[0] + 1
                    end = gc.end_point[0] + 1
                    text = source_bytes[gc.start_byte:gc.end_byte].decode("utf-8", errors="replace")
                    functions.append((name, start, end, text))
    return functions


def analyze(source: str) -> ScopeProfile:
    """Analyze a C++ source string and return a ScopeProfile."""
    parser = Parser(CPP_LANGUAGE)
    tree = parser.parse(source.encode("utf-8"))
    profile = ScopeProfile()
    profile.total_lines = source.count("\n") + (1 if source and not source.endswith("\n") else 0)
    _walk(tree.root_node, 0, profile)
    profile.classify()
    return profile


def analyze_file(path: str | Path) -> ScopeProfile:
    """Analyze a C++ file."""
    source = Path(path).read_text(encoding="utf-8", errors="replace")
    return analyze(source)


def extract_functions(path: str | Path) -> list[FunctionTarget]:
    """Extract all functions from a C++ file with per-function scope profiles.

    Returns functions sorted by optimization potential (highest first).
    """
    filepath = Path(path)
    source = filepath.read_text(encoding="utf-8", errors="replace")
    source_bytes = source.encode("utf-8")

    parser = Parser(CPP_LANGUAGE)
    tree = parser.parse(source_bytes)
    raw_functions = _find_functions(tree.root_node, source_bytes)

    targets = []
    for name, start, end, text in raw_functions:
        profile = analyze(text)
        if profile.skip_reason:
            continue
        # Skip trivial functions (getters, setters, one-liners)
        if profile.statement_count <= 2 and profile.loop_count == 0:
            continue
        targets.append(FunctionTarget(
            name=name,
            start_line=start,
            end_line=end,
            source=text,
            profile=profile,
            file_path=str(filepath),
        ))

    # Sort by optimization potential
    targets.sort(key=lambda t: (
        -t.profile.loop_count * 3
        - t.profile.max_loop_depth * 5
        - t.profile.branches_in_loops * 4
        - t.profile.max_strategies * 2
        - (10 if t.profile.has_nested_loops else 0)
    ))
    return targets


def profile_to_prompt_context(profile: ScopeProfile) -> str:
    """Format a ScopeProfile as a concise prompt injection block."""
    if profile.skip_reason:
        return (
            "## AST Scope Profile\n"
            f"- Skip: {profile.skip_reason}\n"
            f"- Code type: {profile.code_type}, {profile.statement_count} statements\n"
            "- Optimization not recommended for this target."
        )

    lines = [
        "## AST Scope Profile (auto-generated, objective metrics)",
        f"- Code type: {profile.code_type}",
        f"- Scope depth: {profile.scope_depth}",
        f"- Lines: {profile.total_lines}, Statements: {profile.statement_count}",
        f"- Loops: {profile.loop_count} (max nesting: {profile.max_loop_depth}, nested: {profile.has_nested_loops})",
        f"- Innermost loop body: {profile.innermost_loop_statements} statements",
        f"- Branches: {profile.branch_count} ({profile.branches_in_loops} inside loops)",
        f"- Calls: {profile.call_count} ({profile.calls_in_loops} inside loops)",
    ]
    if profile.distinct_callees:
        lines.append(f"- Callees: {', '.join(profile.distinct_callees[:10])}")
    flags = []
    if profile.has_array_subscript:
        flags.append("array_subscript")
    if profile.has_pointer_arithmetic:
        flags.append("pointer_arithmetic")
    if profile.has_float_literal:
        flags.append("floating_point")
    if profile.has_simd_hint:
        flags.append("simd_hint")
    if flags:
        lines.append(f"- Flags: {', '.join(flags)}")
    lines.append(f"- Strategy budget: {profile.max_strategies}")
    lines.append(f"- Recommended strategies: {', '.join(profile.recommended_strategies)}")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m tools.cpp_perf_campaign.scope_analyzer [--functions] <file_or_snippet>")
        sys.exit(1)

    show_functions = "--functions" in sys.argv
    arg = [a for a in sys.argv[1:] if a != "--functions"][0]
    path = Path(arg)

    if show_functions and path.exists():
        targets = extract_functions(path)
        print(f"Found {len(targets)} optimizable functions in {path.name}\n")
        for i, t in enumerate(targets):
            p = t.profile
            print(f"{i+1:2d}. {t.name}  L{t.line_range}  [{p.scope_depth}]  "
                  f"loops={p.loop_count} nest={p.max_loop_depth} br={p.branches_in_loops} "
                  f"strats={p.max_strategies}")
            print(f"    → {', '.join(p.recommended_strategies)}")
        return

    if path.exists():
        profile = analyze_file(path)
    else:
        profile = analyze(arg)

    print(json.dumps(asdict(profile), indent=2))
    print()
    print(profile_to_prompt_context(profile))


if __name__ == "__main__":
    main()
