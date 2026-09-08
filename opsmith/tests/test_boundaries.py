"""The UI boundary rule: only `opsmith/cli/` may import a terminal library.

Core modules talk to a person through the interaction API and report progress through the
event sink; they never import a prompt or a printer. The rule lands with an allowlist of the
modules that still violate it, and each later part of phase 0 deletes its own entries. The
list can only shrink: a module that no longer violates the rule must be removed from it.
"""

import ast
from pathlib import Path
from typing import Dict, List, Set

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

#: Top level packages that only the CLI layer may import.
BANNED_MODULES: Set[str] = {"click", "inquirer", "typer"}

#: Submodules of rich that write to a terminal. `rich.markup` and `rich.text` are pure.
BANNED_RICH_SUBMODULES: Set[str] = {"console", "prompt", "status"}

#: Names that may not be imported from `rich` itself, as in `from rich import print`.
BANNED_RICH_NAMES: Set[str] = {"print"}

#: Directories inside the package that the rule does not apply to.
EXEMPT_DIRECTORIES: Set[str] = {"cli", "tests"}

#: Modules that still import a terminal library, with the part of phase 0 that removes them.
ALLOWED_UI_IMPORTS: Dict[str, Set[str]] = {
    "cloud_providers/aws.py": {"inquirer"},  # 0d
    "cloud_providers/gcp.py": {"inquirer"},  # 0d
    "deployment_strategies/monolithic.py": {"inquirer"},  # 0d
    "service_detector.py": {"inquirer"},  # 0d
}


def _ui_imports(module_path: Path) -> Set[str]:
    """
    Reports which terminal libraries a module imports.

    :param module_path: The module to inspect.
    :return: Labels such as "inquirer" or "rich.print", empty when the module is clean.
    """
    found: Set[str] = set()
    tree = ast.parse(module_path.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root, _, submodule = alias.name.partition(".")
                if root in BANNED_MODULES:
                    found.add(root)
                elif root == "rich" and submodule in BANNED_RICH_SUBMODULES:
                    found.add(f"rich.{submodule}")

        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            root, _, submodule = node.module.partition(".")
            if root in BANNED_MODULES:
                found.add(root)
            elif root == "rich":
                if submodule in BANNED_RICH_SUBMODULES:
                    found.add(f"rich.{submodule}")
                for alias in node.names:
                    if not submodule and alias.name in BANNED_RICH_NAMES:
                        found.add(f"rich.{alias.name}")

    return found


def _modules_under_the_rule() -> List[Path]:
    """
    Lists every package module the boundary rule applies to.

    :return: Module paths, excluding the CLI layer and the tests.
    """
    modules = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT)
        if relative.parts[0] in EXEMPT_DIRECTORIES:
            continue
        modules.append(path)
    return modules


def test_no_new_ui_imports_outside_the_cli_layer():
    """
    Walks every module outside opsmith/cli/ and fails on any terminal library import that
    the allowlist does not already record.
    """
    violations = {}
    for module_path in _modules_under_the_rule():
        relative = str(module_path.relative_to(PACKAGE_ROOT))
        unexpected = _ui_imports(module_path) - ALLOWED_UI_IMPORTS.get(relative, set())
        if unexpected:
            violations[relative] = sorted(unexpected)

    assert not violations, (
        "Modules outside opsmith/cli/ must not import a terminal library. Move the code into"
        f" opsmith/cli/ or emit an event instead: {violations}"
    )


def test_the_allowlist_has_no_stale_entries():
    """
    Fails when a module in the allowlist no longer imports what it is allowed to, so a part
    that cleans a module up is forced to shrink the list rather than leave it stale.
    """
    stale = {}
    for relative, allowed in ALLOWED_UI_IMPORTS.items():
        module_path = PACKAGE_ROOT / relative
        assert module_path.exists(), f"{relative} is in the allowlist but does not exist"

        no_longer_imported = allowed - _ui_imports(module_path)
        if no_longer_imported:
            stale[relative] = sorted(no_longer_imported)

    assert not stale, f"Remove these cleaned up entries from ALLOWED_UI_IMPORTS: {stale}"


def test_the_cli_layer_is_the_only_exempt_code():
    """
    Guards the exemption itself: only opsmith/cli/ and the tests are outside the rule, so a
    new package cannot quietly be added to the exempt list without this failing.
    """
    assert EXEMPT_DIRECTORIES == {"cli", "tests"}
