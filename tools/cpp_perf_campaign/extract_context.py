"""Extract minimal compilation context for a C++ function.

Given a source file, function name, and compile_commands.json, produces:
  1. compile_flags.txt  — the -I/-D flags needed to compile
  2. function_context.h — minimal header with type definitions the function depends on

Usage:
    python3 -m tools.cpp_perf_campaign.extract_context \
        --compile-commands /path/to/compile_commands.json \
        --source src/execution/join_hashtable.cpp \
        --function InsertHashesLoop \
        --output-dir /tmp/context_out
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

import tree_sitter_cpp as tscpp
from tree_sitter import Language, Parser

CPP_LANGUAGE = Language(tscpp.language())

# Types we never need to extract (built-in or standard library)
BUILTIN_TYPES = {
    "void", "bool", "char", "short", "int", "long", "float", "double",
    "signed", "unsigned", "size_t", "ssize_t", "ptrdiff_t", "intptr_t",
    "uintptr_t", "int8_t", "int16_t", "int32_t", "int64_t",
    "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "nullptr_t", "auto", "decltype",
    "string", "vector", "array", "map", "unordered_map", "set",
    "unordered_set", "pair", "tuple", "optional", "variant",
    "unique_ptr", "shared_ptr", "function", "atomic",
    "mutex", "lock_guard", "unique_lock",
    "true", "false", "NULL", "nullptr",
}

# ── Step 1: Extract compile flags ──────────────────────────────────────

def extract_compile_flags(
    compile_commands_path: Path,
    source_file: str,
    repo_root: Path,
) -> tuple[list[str], list[str]]:
    """Extract -I and -D flags for a source file from compile_commands.json."""
    cc = json.loads(compile_commands_path.read_text(encoding="utf-8"))

    # Normalize source path for matching
    source_abs = (repo_root / source_file).resolve()

    for entry in cc:
        entry_file = Path(entry["file"]).resolve()
        if entry_file == source_abs:
            parts = shlex.split(entry.get("command", ""))
            includes = []
            defines = []
            i = 0
            while i < len(parts):
                p = parts[i]
                if p.startswith("-I"):
                    if p == "-I" and i + 1 < len(parts):
                        includes.append(f"-I{parts[i+1]}")
                        i += 2
                    else:
                        includes.append(p)
                        i += 1
                elif p.startswith("-D"):
                    if p == "-D" and i + 1 < len(parts):
                        defines.append(f"-D{parts[i+1]}")
                        i += 2
                    else:
                        defines.append(p)
                        i += 1
                else:
                    i += 1
            return includes, defines

    raise FileNotFoundError(
        f"Source file '{source_file}' not found in compile_commands.json"
    )


def write_compile_flags(includes: list[str], defines: list[str], output_dir: Path) -> Path:
    """Write compile_flags.txt."""
    path = output_dir / "compile_flags.txt"
    flags = includes + defines + ["-std=c++17", "-O2"]
    path.write_text(" ".join(flags) + "\n", encoding="utf-8")
    return path


# ── Step 2: Extract type names from function ───────────────────────────

def _collect_type_identifiers(node, types: set[str]) -> None:
    """Walk AST and collect identifiers that appear in type positions."""
    t = node.type

    # Direct type references
    if t in ("type_identifier", "qualified_identifier", "namespace_identifier"):
        name = node.text.decode("utf-8", errors="replace")
        # Strip template args for lookup
        base = name.split("<")[0].split("::")[-1].strip()
        if base and base not in BUILTIN_TYPES and not base.startswith("std::"):
            types.add(base)
        # Also keep qualified name for context
        if "::" in name:
            parts = name.split("::")
            for part in parts:
                clean = part.split("<")[0].strip()
                if clean and clean not in BUILTIN_TYPES:
                    types.add(clean)

    # Template arguments often contain type names
    if t == "template_argument_list":
        for child in node.children:
            _collect_type_identifiers(child, types)
        return

    for child in node.children:
        _collect_type_identifiers(child, types)


def extract_type_names(source: str, function_name: str) -> set[str]:
    """Extract type names used in a specific function."""
    parser = Parser(CPP_LANGUAGE)
    tree = parser.parse(source.encode("utf-8"))

    # Find the function
    types: set[str] = set()

    def find_func(node):
        if node.type == "function_definition":
            text = node.text.decode("utf-8", errors="replace")
            if function_name in text:
                _collect_type_identifiers(node, types)
                return True
        for child in node.children:
            if find_func(child):
                return True
        return False

    find_func(tree.root_node)
    return types


# ── Step 3: Find type definitions in headers ───────────────────────────

# Patterns that define a type
TYPE_DEF_PATTERNS = [
    r"^\s*(?:class|struct|enum)\s+{name}\b",
    r"^\s*using\s+{name}\s*=",
    r"^\s*typedef\s+.*\b{name}\s*;",
    r"^\s*enum\s+class\s+{name}\b",
]


def _find_type_in_file(header: Path, type_name: str) -> str | None:
    """Search a header file for a type definition.  Returns the definition block or None."""
    try:
        content = header.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return None

    for pattern_template in TYPE_DEF_PATTERNS:
        pattern = pattern_template.format(name=re.escape(type_name))
        match = re.search(pattern, content, re.MULTILINE)
        if match:
            # Extract the full definition (up to closing brace or semicolon)
            start = match.start()
            # For class/struct/enum, find matching brace
            if any(kw in match.group() for kw in ("class", "struct", "enum")):
                brace_start = content.find("{", start)
                if brace_start == -1:
                    # Forward declaration — find the semicolon
                    end = content.find(";", start)
                    if end != -1:
                        return content[start:end+1].strip()
                    continue
                depth = 0
                pos = brace_start
                while pos < len(content):
                    if content[pos] == "{":
                        depth += 1
                    elif content[pos] == "}":
                        depth -= 1
                        if depth == 0:
                            end = content.find(";", pos)
                            if end == -1:
                                end = pos + 1
                            return content[start:end+1].strip()
                    pos += 1
            else:
                # typedef or using — find semicolon
                end = content.find(";", start)
                if end != -1:
                    return content[start:end+1].strip()
    return None


def find_type_definitions(
    type_names: set[str],
    include_dirs: list[str],
    repo_root: Path,
    max_depth: int = 2,
) -> dict[str, tuple[str, str]]:
    """Find definitions for type names in include directories.

    Returns {type_name: (header_path, definition_text)}.
    Does transitive resolution up to max_depth levels.
    """
    results: dict[str, tuple[str, str]] = {}
    pending = set(type_names)
    searched: set[str] = set()

    # Collect all header files from include dirs
    headers: list[Path] = []
    for inc in include_dirs:
        inc_path = Path(inc.lstrip("-I"))
        if not inc_path.is_absolute():
            inc_path = repo_root / inc_path
        if inc_path.is_dir():
            headers.extend(inc_path.rglob("*.h"))
            headers.extend(inc_path.rglob("*.hpp"))

    for _depth in range(max_depth + 1):
        if not pending:
            break
        next_pending: set[str] = set()
        for type_name in pending:
            if type_name in searched or type_name in BUILTIN_TYPES:
                continue
            searched.add(type_name)
            for header in headers:
                defn = _find_type_in_file(header, type_name)
                if defn:
                    results[type_name] = (str(header), defn)
                    # Find types referenced in this definition (transitive)
                    parser = Parser(CPP_LANGUAGE)
                    tree = parser.parse(defn.encode("utf-8"))
                    inner_types: set[str] = set()
                    _collect_type_identifiers(tree.root_node, inner_types)
                    for it in inner_types:
                        if it not in results and it not in searched:
                            next_pending.add(it)
                    break
        pending = next_pending

    return results


# ── Step 4: Generate function_context.h ────────────────────────────────

def generate_context_header(
    type_defs: dict[str, tuple[str, str]],
    function_source: str,
    output_dir: Path,
) -> Path:
    """Generate a minimal function_context.h."""
    path = output_dir / "function_context.h"
    lines = [
        "#pragma once",
        "// Auto-generated minimal context for standalone benchmark compilation",
        "// Contains only the type definitions needed by the target function.",
        "",
        "#include <cstdint>",
        "#include <cstddef>",
        "#include <cstring>",
        "#include <vector>",
        "#include <array>",
        "#include <algorithm>",
        "#include <functional>",
        "#include <memory>",
        "",
    ]

    # Group by source header for readability
    by_header: dict[str, list[tuple[str, str]]] = {}
    for type_name, (header, defn) in type_defs.items():
        short_header = header.split("/include/")[-1] if "/include/" in header else header
        by_header.setdefault(short_header, []).append((type_name, defn))

    for header, defs in sorted(by_header.items()):
        lines.append(f"// From: {header}")
        for type_name, defn in defs:
            lines.append(f"// Type: {type_name}")
            lines.append(defn)
            lines.append("")

    lines.append("// --- Target function ---")
    lines.append(function_source)
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ── Orchestrator ───────────────────────────────────────────────────────

def _find_header_for_type(
    type_name: str,
    include_dirs: list[str],
    repo_root: Path,
) -> str | None:
    """Find which header defines a type (shallow search, no recursion)."""
    for inc in include_dirs:
        inc_path = Path(inc.lstrip("-I"))
        if not inc_path.is_absolute():
            inc_path = repo_root / inc_path
        if not inc_path.is_dir():
            continue
        for header in inc_path.rglob("*.h*"):
            try:
                content = header.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for pat in TYPE_DEF_PATTERNS:
                if re.search(pat.format(name=re.escape(type_name)), content, re.MULTILINE):
                    # Return as include-friendly path relative to the -I dir
                    try:
                        rel = header.relative_to(inc_path)
                        return str(rel)
                    except ValueError:
                        return str(header)
    return None


def _find_source_includes(source: str) -> list[str]:
    """Extract #include "..." directives from source."""
    return re.findall(r'#include\s*"([^"]+)"', source)


def extract_context(
    compile_commands_path: Path,
    source_file: str,
    function_name: str,
    repo_root: Path,
    output_dir: Path,
) -> dict[str, object]:
    """Extract compilation context for standalone benchmark construction.

    Produces:
    - compile_flags.txt: -I/-D flags from compile_commands.json
    - extraction_meta.json: function info, type-to-header mapping, source includes

    Does NOT produce a giant context header. Instead gives the agent
    enough information to write correct #include directives.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Compile flags
    includes, defines = extract_compile_flags(compile_commands_path, source_file, repo_root)
    flags_path = write_compile_flags(includes, defines, output_dir)

    # 2. Read source and extract function
    source_path = repo_root / source_file
    source = source_path.read_text(encoding="utf-8", errors="replace")

    from .scope_analyzer import extract_functions
    functions = extract_functions(source_path)
    match = [f for f in functions if f.name == function_name]
    if not match:
        match = [f for f in functions if function_name in f.name]
    if not match:
        return {"error": f"Function '{function_name}' not found"}
    func = match[0]

    # 3. Extract type names used in the function (first level only)
    type_names = extract_type_names(source, function_name)

    # 4. Map each type to its header (shallow — no transitive closure)
    type_headers: dict[str, str] = {}
    for tn in sorted(type_names):
        header = _find_header_for_type(tn, includes, repo_root)
        if header:
            type_headers[tn] = header

    # 5. Source file's own includes (useful context for the agent)
    source_includes = _find_source_includes(source)

    # 6. Write metadata
    meta = {
        "source_file": source_file,
        "function_name": function_name,
        "function_lines": func.line_range,
        "function_source": func.source,
        "type_names": sorted(type_names),
        "type_headers": type_headers,
        "types_unresolved": sorted(type_names - set(type_headers.keys())),
        "source_includes": source_includes,
        "compile_flags_file": str(flags_path),
        "compile_flags_inline": " ".join(includes + defines + ["-std=c++17", "-O2"]),
        "hint": (
            "Use compile_flags_inline with c++ to compile standalone benchmarks. "
            "Include the headers listed in type_headers to get type definitions. "
            "Do NOT build the whole project."
        ),
    }
    (output_dir / "extraction_meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    return meta


# ── CLI ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Extract minimal compilation context for a C++ function")
    parser.add_argument("--compile-commands", required=True, help="Path to compile_commands.json")
    parser.add_argument("--source", required=True, help="Source file (relative to repo root)")
    parser.add_argument("--function", required=True, help="Function name to extract context for")
    parser.add_argument("--repo-root", default=None, help="Repository root (default: parent of compile_commands.json)")
    parser.add_argument("--output-dir", required=True, help="Output directory")
    args = parser.parse_args()

    cc_path = Path(args.compile_commands).resolve()
    repo_root = Path(args.repo_root).resolve() if args.repo_root else cc_path.parent.parent
    output_dir = Path(args.output_dir).resolve()

    meta = extract_context(cc_path, args.source, args.function, repo_root, output_dir)
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
