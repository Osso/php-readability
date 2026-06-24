#!/usr/bin/env python3
"""Readability audit for PHP repos.

Usage: audit-repo.py [path] [--fix] [--json] [--write-plan] [--exclude dir1,dir2]
  path         defaults to current directory
  --fix        output PLAN.md-style checkbox items
  --json       output machine-readable JSON
  --write-plan append new items to repo PLAN.md (deduped)
  --exclude    comma-separated dirs to skip (for example: vendor,cache)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from fnmatch import fnmatch
from pathlib import Path

MAX_BODY_LINES = 30
MAX_BODY_LINES_TEST = 200
MAX_NESTING = 4
MAX_FILE_LINES = 750
MAX_PARAMS = 5
COMPLEX_COND_OPERATORS = 3
SIBLING_SIMILARITY = 0.75
MAX_COGNITIVE = 15
MAX_CYCLOMATIC = 20

# rust-code-analysis-cli computes real cognitive + cyclomatic complexity for PHP
# (added in the local fork). When present we use it instead of the line-based
# heuristic; otherwise we fall back to the approximation below.
RCA_BIN = shutil.which("rust-code-analysis-cli")

EXCLUDE_DIRS: set[str] = {
    "vendor",
    "node_modules",
    "storage",
    "cache",
    ".git",
    ".worktrees",
    ".claude/worktrees",
}
EXCLUDE_PATTERNS: list[str] = []
GENERATED_PREFIXES = ("generated_",)

FUNCTION_RE = re.compile(
    r"^\s*(?:final\s+|abstract\s+)?(?:public|protected|private)?\s*"
    r"(?:static\s+)?function\s+&?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)"
)
CONTROL_RE = re.compile(r"\b(if|elseif|for|foreach|while|switch|catch|match)\b")
COMPLEX_COND_RE = re.compile(r"^\s*(?:if|elseif|while)\s*\((.*)\)")


@dataclass
class Issue:
    category: str
    file: str
    line: int
    function: str | None
    problem: str
    fix: str


@dataclass
class FunctionInfo:
    name: str
    line: int
    end_line: int
    body_lines: int
    nesting: int
    params: list[str]
    normalized_body: list[str]


def load_ignore_file(root: Path) -> None:
    ignore_path = root / ".readability-ignore"
    if not ignore_path.exists():
        return

    for raw_line in ignore_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "/" in line and "*" not in line and "?" not in line and "." not in Path(line).name:
            EXCLUDE_DIRS.add(line.rstrip("/"))
            continue
        EXCLUDE_PATTERNS.append(line)


def is_excluded_by_pattern(rel: str) -> bool:
    return any(fnmatch(rel, pat) or fnmatch(os.path.basename(rel), pat) for pat in EXCLUDE_PATTERNS)


def is_generated_file(filename: str) -> bool:
    lowered = filename.lower()
    return any(lowered.startswith(prefix) for prefix in GENERATED_PREFIXES)


def find_php_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*.php")):
        rel = str(path.relative_to(root))
        if any(rel == d or rel.startswith(f"{d}/") for d in EXCLUDE_DIRS):
            continue
        if "/vendor/" in rel or rel.startswith("vendor/"):
            continue
        if is_generated_file(path.name):
            continue
        if is_excluded_by_pattern(rel):
            continue
        files.append(path)
    return files


def is_test_file(path: Path, root: Path) -> bool:
    rel = str(path.relative_to(root)).lower()
    return "/test" in rel or rel.startswith("tests/") or rel.endswith("test.php")


def normalize_line(line: str) -> str:
    line = re.sub(r"//.*$", "", line)
    line = re.sub(r"#.*$", "", line)
    line = re.sub(r"/\*.*?\*/", "", line)
    line = re.sub(r"'[^']*'", "'STR'", line)
    line = re.sub(r'"[^"]*"', '"STR"', line)
    line = re.sub(r"\b\d+\b", "NUM", line)
    return re.sub(r"\s+", " ", line.strip())


def check_file_length(path: Path, lines: list[str], root: Path) -> list[Issue]:
    if len(lines) <= MAX_FILE_LINES:
        return []
    return [
        Issue(
            category="LENGTH",
            file=str(path.relative_to(root)),
            line=1,
            function=None,
            problem=f"File is {len(lines)} lines (max {MAX_FILE_LINES})",
            fix="Split the file into smaller classes, traits, or helpers",
        )
    ]


def parse_functions(path: Path, lines: list[str], root: Path) -> tuple[list[Issue], list[FunctionInfo]]:
    issues: list[Issue] = []
    functions: list[FunctionInfo] = []
    i = 0
    while i < len(lines):
        match = FUNCTION_RE.match(lines[i])
        if not match:
            i += 1
            continue

        name = match.group(1)
        params = split_params(match.group(2))
        open_line = find_opening_brace(lines, i)
        if open_line is None:
            i += 1
            continue

        body_lines, nesting, end_line, normalized_body = scan_function_body(lines, open_line)
        limit = MAX_BODY_LINES_TEST if is_test_file(path, root) else MAX_BODY_LINES
        rel = str(path.relative_to(root))

        functions.append(
            FunctionInfo(
                name=name,
                line=i + 1,
                end_line=end_line + 1,
                body_lines=body_lines,
                nesting=nesting,
                params=params,
                normalized_body=normalized_body,
            )
        )

        if body_lines > limit:
            issues.append(
                Issue(
                    category="LENGTH",
                    file=rel,
                    line=i + 1,
                    function=name,
                    problem=f"{body_lines} body lines (max {limit})",
                    fix="Extract sequential steps into named helpers",
                )
            )

        if nesting > MAX_NESTING and not is_test_file(path, root):
            issues.append(
                Issue(
                    category="NESTING",
                    file=rel,
                    line=i + 1,
                    function=name,
                    problem=f"Nesting depth {nesting} (max {MAX_NESTING})",
                    fix="Use guard clauses or extract inner branches",
                )
            )

        if count_primitive_params(params) >= MAX_PARAMS:
            issues.append(
                Issue(
                    category="PARAM_OVERLOAD",
                    file=rel,
                    line=i + 1,
                    function=name,
                    problem=f"{count_primitive_params(params)} primitive-style parameters (max {MAX_PARAMS - 1})",
                    fix="Group related inputs into a value object, DTO, or context array with documented keys",
                )
            )

        i = end_line + 1

    return issues, functions


def split_params(raw_params: str) -> list[str]:
    if not raw_params.strip():
        return []

    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in raw_params:
        if char in "([<":
            depth += 1
        elif char in ")]>":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current).strip())
    return parts


def count_primitive_params(params: list[str]) -> int:
    primitive_pattern = re.compile(
        r"^(?:\??(?:string|int|float|bool|array|iterable|mixed|scalar|callable)|\$)"
    )
    count = 0
    for param in params:
        without_default = param.split("=", 1)[0].strip()
        normalized = without_default.replace("&", "").replace("...", "").strip()
        if primitive_pattern.match(normalized) or "$" in normalized and " " not in normalized:
            count += 1
    return count


def find_opening_brace(lines: list[str], start: int) -> int | None:
    for idx in range(start, min(len(lines), start + 12)):
        if "{" in lines[idx]:
            return idx
        if ";" in lines[idx]:
            return None
    return None


def scan_function_body(lines: list[str], open_line: int) -> tuple[int, int, int, list[str]]:
    brace_depth = 0
    max_depth = 0
    body_lines = 0
    opened = False
    normalized_body: list[str] = []
    end_line = open_line

    for idx in range(open_line, len(lines)):
        line = lines[idx]
        for char in line:
            if char == "{":
                brace_depth += 1
                opened = True
                max_depth = max(max_depth, brace_depth)
            elif char == "}":
                brace_depth -= 1

        stripped = line.strip()
        if opened and idx > open_line and brace_depth > 0:
            if should_count_body_line(stripped):
                body_lines += 1
                normalized = normalize_line(line)
                if normalized:
                    normalized_body.append(normalized)

        if opened and brace_depth == 0:
            end_line = idx
            break

    return body_lines, max(0, max_depth - 1), end_line, normalized_body


def should_count_body_line(stripped: str) -> bool:
    if not stripped:
        return False
    if stripped.startswith("//") or stripped.startswith("#") or stripped.startswith("/*") or stripped.startswith("*"):
        return False
    return stripped not in {"{", "}", "};", "},", "];", ");"}


def check_suppressions(path: Path, lines: list[str], root: Path) -> list[Issue]:
    patterns = {
        "@phpstan-ignore-line": "Remove the ignore and fix the underlying PHPStan error",
        "@phpstan-ignore-next-line": "Remove the ignore and fix the underlying PHPStan error",
        "@phpstan-ignore": "Replace the broad suppression with a real type or control-flow fix",
        "@psalm-suppress": "Remove the suppression and fix the underlying Psalm issue",
        "phpcs:ignore": "Remove the ignore and satisfy the coding standard",
        "phpcs:disable": "Limit the scope or remove the disable and fix the root issue",
        "@noinspection": "Remove the IDE suppression and make the code explicit",
    }
    issues: list[Issue] = []
    rel = str(path.relative_to(root))
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        for marker, fix in patterns.items():
            if marker in stripped:
                issues.append(
                    Issue(
                        category="SUPPRESS",
                        file=rel,
                        line=index,
                        function=None,
                        problem=f"Suppression marker `{marker}` hides a readability or analysis issue",
                        fix=fix,
                    )
                )
        if re.search(r"(^|[^$@])@\s*[A-Za-z_][A-Za-z0-9_]*\s*\(", stripped):
            issues.append(
                Issue(
                    category="SUPPRESS",
                    file=rel,
                    line=index,
                    function=None,
                    problem="Error suppression with `@` on a function call",
                    fix="Handle the failure explicitly instead of suppressing it",
                )
            )
    return issues


def check_complex_conditions(path: Path, lines: list[str], root: Path) -> list[Issue]:
    issues: list[Issue] = []
    rel = str(path.relative_to(root))
    for index, line in enumerate(lines, start=1):
        match = COMPLEX_COND_RE.match(line)
        if not match:
            continue
        condition = match.group(1)
        operator_count = (
            condition.count("&&")
            + condition.count("||")
            + condition.count("!")
        )
        if operator_count >= COMPLEX_COND_OPERATORS:
            issues.append(
                Issue(
                    category="COMPLEX_COND",
                    file=rel,
                    line=index,
                    function=None,
                    problem=f"Condition contains {operator_count} boolean operators",
                    fix="Extract named booleans or move the decision into a helper",
                )
            )
    return issues


def check_state_accumulation(path: Path, lines: list[str], root: Path) -> list[Issue]:
    issues: list[Issue] = []
    rel = str(path.relative_to(root))
    empty_array_re = re.compile(r"^\s*(\$\w+)\s*=\s*\[\s*\]\s*;")
    push_re = re.compile(r"(\$\w+)\[\]\s*=")
    by_ref_re = re.compile(r"&\s*\$\w+")

    arrays: dict[str, int] = {}
    pushes: defaultdict[str, int] = defaultdict(int)
    for index, line in enumerate(lines, start=1):
        empty_match = empty_array_re.match(line)
        if empty_match:
            arrays[empty_match.group(1)] = index

        push_match = push_re.search(line)
        if push_match:
            pushes[push_match.group(1)] += 1

        if "function " in line and by_ref_re.search(line):
            issues.append(
                Issue(
                    category="STATE",
                    file=rel,
                    line=index,
                    function=None,
                    problem="Function signature uses by-reference parameters",
                    fix="Return the computed value instead of mutating caller-owned state",
                )
            )

    for var_name, first_line in arrays.items():
        if pushes[var_name] >= 3:
            issues.append(
                Issue(
                    category="STATE",
                    file=rel,
                    line=first_line,
                    function=None,
                    problem=f"{var_name} is built through repeated mutation ({pushes[var_name]} pushes)",
                    fix="Prefer a mapping/filtering pipeline or a dedicated helper that returns the final array",
                )
            )

    return issues


def check_bumpy_road(path: Path, lines: list[str], root: Path, functions: list[FunctionInfo]) -> list[Issue]:
    issues: list[Issue] = []
    rel = str(path.relative_to(root))
    for function in functions:
        chunk_count = 0
        in_chunk = False
        for raw_line in lines[function.line - 1:function.end_line]:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("//") or stripped.startswith("#"):
                if in_chunk:
                    chunk_count += 1
                    in_chunk = False
                continue
            in_chunk = True
        if in_chunk:
            chunk_count += 1
        if chunk_count >= 3 and function.body_lines > 12:
            issues.append(
                Issue(
                    category="BUMPY_ROAD",
                    file=rel,
                    line=function.line,
                    function=function.name,
                    problem=f"Method reads as {chunk_count} sequential chunks",
                    fix="Extract each logical stage into a named helper",
                )
            )
    return issues


def check_sibling_similarity(path: Path, root: Path, functions: list[FunctionInfo]) -> list[Issue]:
    issues: list[Issue] = []
    rel = str(path.relative_to(root))
    for index, left in enumerate(functions):
        if len(left.normalized_body) < 8:
            continue
        left_set = set(left.normalized_body)
        for right in functions[index + 1:]:
            if len(right.normalized_body) < 8:
                continue
            right_set = set(right.normalized_body)
            union = left_set | right_set
            if not union:
                continue
            similarity = len(left_set & right_set) / len(union)
            if similarity >= SIBLING_SIMILARITY:
                issues.append(
                    Issue(
                        category="SIBLING",
                        file=rel,
                        line=left.line,
                        function=left.name,
                        problem=f"`{left.name}` and `{right.name}` share {similarity:.0%} of normalized lines",
                        fix="Extract the shared logic into a helper and keep only the differences in each method",
                    )
                )
    return issues


def check_complexity(path: Path, lines: list[str], root: Path, functions: list[FunctionInfo]) -> list[Issue]:
    """Report functions over complexity thresholds.

    Prefers real cognitive + cyclomatic metrics from rust-code-analysis-cli;
    falls back to a line-based cyclomatic approximation when it is unavailable.
    """
    if RCA_BIN is not None:
        rca_issues = rca_complexity(path, root)
        if rca_issues is not None:
            return rca_issues
    return heuristic_complexity(path, lines, root, functions)


def rca_complexity(path: Path, root: Path) -> list[Issue] | None:
    """Run rust-code-analysis-cli and emit COMPLEXITY issues per function.

    Returns None if the tool fails to produce parsable output, so the caller
    can fall back to the heuristic.
    """
    try:
        result = subprocess.run(
            [RCA_BIN, "-m", "-p", str(path), "-O", "json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        tree = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    rel = str(path.relative_to(root))
    issues: list[Issue] = []

    def walk(node: dict) -> None:
        if node.get("kind") == "function":
            metrics = node.get("metrics", {})
            cognitive = metrics.get("cognitive", {}).get("sum", 0)
            cyclomatic = metrics.get("cyclomatic", {}).get("sum", 0)
            name = node.get("name") or "<anonymous>"
            line = node.get("start_line", 1)
            if cognitive > MAX_COGNITIVE:
                issues.append(
                    Issue(
                        category="COMPLEXITY",
                        file=rel,
                        line=line,
                        function=name,
                        problem=f"Cognitive complexity {cognitive:.0f} (max {MAX_COGNITIVE})",
                        fix="Reduce branching and nesting; extract conditions into named helpers",
                    )
                )
            if cyclomatic > MAX_CYCLOMATIC:
                issues.append(
                    Issue(
                        category="COMPLEXITY",
                        file=rel,
                        line=line,
                        function=name,
                        problem=f"Cyclomatic complexity {cyclomatic:.0f} (max {MAX_CYCLOMATIC})",
                        fix="Reduce the number of code paths; split the method",
                    )
                )
        for child in node.get("spaces", []):
            walk(child)

    walk(tree)
    return issues


def heuristic_complexity(path: Path, lines: list[str], root: Path, functions: list[FunctionInfo]) -> list[Issue]:
    issues: list[Issue] = []
    rel = str(path.relative_to(root))
    for function in functions:
        complexity = 1
        for raw_line in lines[function.line - 1:function.end_line]:
            complexity += len(CONTROL_RE.findall(raw_line))
            complexity += raw_line.count("&&")
            complexity += raw_line.count("||")
            complexity += raw_line.count("?")
        if complexity > MAX_CYCLOMATIC:
            issues.append(
                Issue(
                    category="COMPLEXITY",
                    file=rel,
                    line=function.line,
                    function=function.name,
                    problem=f"Approximate cyclomatic complexity {complexity} (max {MAX_CYCLOMATIC})",
                    fix="Split the method and remove nested branching",
                )
            )
    return issues


def format_issue(issue: Issue) -> str:
    location = f"{issue.file}:{issue.line}"
    suffix = f" — {issue.function}()" if issue.function else ""
    return (
        f"[{issue.category}] {location}{suffix}\n"
        f"  Problem: {issue.problem}\n"
        f"  Fix: {issue.fix}"
    )


def format_fix_item(issue: Issue) -> str:
    location = f"{issue.file}:{issue.line}"
    target = f"{issue.function}()" if issue.function else "file-level issue"
    return f"- [ ] [{issue.category}] {location} {target} — {issue.problem}"


def normalize_plan_checkbox(line: str) -> str:
    return re.sub(r"^- \[[ xX]\]\s+", "- [ ] ", line.strip())


def append_plan(root: Path, issues: list[Issue]) -> int:
    plan_path = root / "PLAN.md"
    if plan_path.exists():
        content = plan_path.read_text()
    else:
        content = "# Project Plan\n\n## Current Blocker\n\nNone\n\n## Active TODO\n\n"

    existing = {
        normalize_plan_checkbox(raw)
        for raw in content.splitlines()
        if raw.strip().startswith("- [")
    }

    new_lines: list[str] = []
    for issue in sorted(issues, key=lambda item: (item.category, item.file, item.line, item.function or "", item.problem)):
        line = format_fix_item(issue)
        normalized = normalize_plan_checkbox(line)
        if normalized in existing:
            continue
        existing.add(normalized)
        new_lines.append(line)

    if not new_lines:
        if not plan_path.exists():
            plan_path.write_text(content)
        return 0

    if content and not content.endswith("\n"):
        content += "\n"

    header = f"## Readability Audit ({date.today().isoformat()})"
    block = f"\n{header}\n\n" + "\n".join(new_lines) + "\n"
    plan_path.write_text(content + block)
    return len(new_lines)


def main(argv: list[str]) -> int:
    args = argv[1:]
    root = Path(".")
    json_mode = False
    fix_mode = False
    write_plan_mode = False

    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg == "--json":
            json_mode = True
        elif arg == "--fix":
            fix_mode = True
        elif arg == "--write-plan":
            write_plan_mode = True
        elif arg == "--exclude" and idx + 1 < len(args):
            EXCLUDE_DIRS.update(part.strip().strip("/") for part in args[idx + 1].split(",") if part.strip())
            idx += 1
        elif arg.startswith("--"):
            print(f"Unknown flag: {arg}", file=sys.stderr)
            return 2
        else:
            root = Path(arg)
        idx += 1

    root = root.resolve()
    load_ignore_file(root)

    issues: list[Issue] = []
    for path in find_php_files(root):
        try:
            lines = path.read_text().splitlines()
        except UnicodeDecodeError:
            continue

        issues.extend(check_file_length(path, lines, root))
        issues.extend(check_suppressions(path, lines, root))
        issues.extend(check_complex_conditions(path, lines, root))
        issues.extend(check_state_accumulation(path, lines, root))

        function_issues, functions = parse_functions(path, lines, root)
        issues.extend(function_issues)
        issues.extend(check_complexity(path, lines, root, functions))
        issues.extend(check_bumpy_road(path, lines, root, functions))
        issues.extend(check_sibling_similarity(path, root, functions))

    issues.sort(key=lambda issue: (issue.file, issue.line, issue.category, issue.function or ""))

    if write_plan_mode:
        added = append_plan(root, issues)
        print(f"Wrote {added} new item(s) to PLAN.md")
        return 0

    if json_mode:
        print(json.dumps([issue.__dict__ for issue in issues], indent=2))
        return 0

    if fix_mode:
        for issue in issues:
            print(format_fix_item(issue))
        return 0

    for issue in issues:
        print(format_issue(issue))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
