"""Keep package dependencies pointed from policy toward the UI boundary."""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "threads_browser"

CORE_FILE_LIMITS = {
    "reader.py": 600,
    "browser_ui/home_navigation.py": 600,
    "browser_ui/viewport_scan.py": 600,
    "browser_ui/viewport_scan_pipeline.py": 500,
    "browser_ui/body_text.py": 600,
    "browser_ui/body_progress.py": 400,
    "browser_ui/body_progress_state.py": 500,
    "browser_ui/viewport_row_verifier.py": 500,
    "browser_ui/root_discovery.py": 500,
    "nurture/engine.py": 300,
    "nurture/scan_cycle.py": 500,
    "nurture/home_observation.py": 500,
}

CORE_FUNCTION_LIMITS = {
    "nurture/engine.py": {"_run_page_workflow_cycle": 120},
    "nurture/scan_cycle.py": {"scan_home_feed": 90},
    "browser_ui/viewport_scan_pipeline.py": {"_scan_active": 100},
    "browser_ui/viewport_row_verifier.py": {"_process_candidate": 100},
    "browser_ui/home_navigation.py": {"open_home": 80},
    "browser_ui/body_progress.py": {"consume": 100},
}


def _module_name(path: Path) -> str:
    return ".".join(("threads_browser", *path.relative_to(SOURCE_ROOT).with_suffix("").parts))


def _local_import_targets(module: str, tree: ast.Module) -> set[str]:
    package = module.split(".")[:-1]
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(
                alias.name
                for alias in node.names
                if alias.name == "threads_browser"
                or alias.name.startswith("threads_browser.")
            )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                if node.module:
                    targets.add(".".join((*base, *node.module.split("."))))
                else:
                    targets.update(
                        ".".join((*base, *alias.name.split(".")))
                        for alias in node.names
                        if alias.name != "*"
                    )
            elif node.module and (
                node.module == "threads_browser"
                or node.module.startswith("threads_browser.")
            ):
                targets.add(node.module)
    return targets


def test_package_imports_remain_acyclic_and_layered() -> None:
    modules = {
        _module_name(path): ast.parse(path.read_text(encoding="utf-8"))
        for path in SOURCE_ROOT.rglob("*.py")
    }
    edges = {
        module: {
            target for target in _local_import_targets(module, tree) if target in modules
        }
        for module, tree in modules.items()
    }

    def visit(module: str, active: tuple[str, ...], visited: set[str]) -> None:
        assert module not in active, "circular local import: " + " -> ".join(
            (*active, module)
        )
        if module in visited:
            return
        visited.add(module)
        for dependency in edges[module]:
            visit(dependency, (*active, module), visited)

    visited: set[str] = set()
    for module in edges:
        visit(module, (), visited)

    forbidden = {
        "feed": {"browser_ui", "reader", "nurture", "storage", "workflow", "cli"},
        "browser_ui": {"reader", "nurture", "storage", "workflow", "cli"},
        "reader": {"nurture", "storage", "workflow"},
        "nurture": {"workflow", "cli"},
        "persistence": {"browser_ui", "feed", "reader", "nurture", "workflow", "cli"},
    }
    for module, tree in modules.items():
        package = module.split(".")[1]
        blocked = forbidden.get(package, set())
        for target in _local_import_targets(module, tree):
            parts = target.split(".")
            if len(parts) > 1 and parts[0] == "threads_browser":
                assert parts[1] not in blocked, f"{module} must not depend on {target}"


def test_core_ownership_modules_remain_reviewable() -> None:
    """Keep the issue-targeted coordinators bounded as responsibilities evolve."""
    for relative_path, maximum in CORE_FILE_LIMITS.items():
        source = SOURCE_ROOT / relative_path
        assert len(source.read_text(encoding="utf-8").splitlines()) <= maximum, (
            f"{relative_path} grew past its {maximum}-line review budget; "
            "split the new responsibility into a cohesive module"
        )

    for relative_path, functions in CORE_FUNCTION_LIMITS.items():
        source = SOURCE_ROOT / relative_path
        tree = ast.parse(source.read_text(encoding="utf-8"))
        definitions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for name, maximum in functions.items():
            matching = [node for node in definitions if node.name == name]
            assert len(matching) == 1, f"expected one {name} in {relative_path}"
            node = matching[0]
            line_count = node.end_lineno - node.lineno + 1
            assert line_count <= maximum, (
                f"{relative_path}:{name} is {line_count} lines; "
                f"split its lifecycle phases below {maximum} lines"
            )
