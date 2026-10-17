"""Check incremental production module and orchestration growth against a base ref."""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "threads_browser"
MODULE_LIMIT = 600
FUNCTION_LIMIT = 150


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def _changed_sources(base: str) -> set[str]:
    changed = {
        line.split("\t", maxsplit=1)[-1]
        for line in _git("diff", "--name-status", "--no-renames", base, "--", "src/threads_browser").splitlines()
        if line and not line.startswith("D\t")
    }
    base_sources = set(
        _git("ls-tree", "-r", "--name-only", base, "--", "src/threads_browser").splitlines()
    )
    current_sources = {
        path.relative_to(SOURCE_ROOT.parents[1]).as_posix()
        for path in SOURCE_ROOT.rglob("*.py")
    }
    return changed | (current_sources - base_sources)


def _function_spans(source: str) -> dict[str, int]:
    tree = ast.parse(source)
    spans: dict[str, int] = {}

    def visit(node: ast.AST, scope: tuple[str, ...] = ()) -> None:
        if isinstance(node, ast.ClassDef):
            scope = (*scope, node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = ".".join((*scope, node.name))
            spans[name] = node.end_lineno - node.lineno + 1
            scope = (*scope, node.name)
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree)
    return spans


def _base_source(base: str, path: str) -> str | None:
    result = subprocess.run(
        ("git", "show", f"{base}:{path}"),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout if result.returncode == 0 else None


def check(base: str) -> tuple[str, ...]:
    failures: list[str] = []
    for relative_path in sorted(_changed_sources(base)):
        path = Path(".") / relative_path
        current_source = path.read_text(encoding="utf-8")
        current_lines = len(current_source.splitlines())
        previous_source = _base_source(base, relative_path)
        if current_lines > MODULE_LIMIT and (
            previous_source is None or current_lines > len(previous_source.splitlines())
        ):
            failures.append(
                f"{relative_path}: {current_lines} lines; new modules must be <= {MODULE_LIMIT} "
                "and existing larger modules must not grow"
            )

        previous_functions = (
            {} if previous_source is None else _function_spans(previous_source)
        )
        for name, current_span in _function_spans(current_source).items():
            if current_span <= FUNCTION_LIMIT:
                continue
            previous_span = previous_functions.get(name)
            if previous_span is None or current_span > previous_span:
                failures.append(
                    f"{relative_path}:{name}: {current_span} lines; new orchestration "
                    f"functions must be <= {FUNCTION_LIMIT} and existing larger functions "
                    "must not grow"
                )
    return tuple(failures)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="base ref to compare (defaults to PR base or HEAD^)")
    args = parser.parse_args()
    base = args.base or (
        f"origin/{os.environ['GITHUB_BASE_REF']}"
        if os.environ.get("GITHUB_EVENT_NAME") == "pull_request"
        else "HEAD^"
    )
    try:
        failures = check(base)
    except (OSError, subprocess.CalledProcessError, SyntaxError) as error:
        print(f"Architecture budget check could not inspect {base}: {type(error).__name__}", file=sys.stderr)
        return 2
    if failures:
        print("Architecture budget violations:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"Architecture budgets pass against {base}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
