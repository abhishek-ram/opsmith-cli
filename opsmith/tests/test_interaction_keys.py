"""Every interaction key used in the code is one the migration plan declares.

Keys are the whole point of the interaction API: a flag, an answers file or a driving agent can
only supply an answer for a question it can name. So the names cannot be invented at the call
site - they come from one table, and this walks the package to prove it.
"""

import ast
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
KEY_TABLE = PACKAGE_ROOT.parent / "docs" / "notes" / "2026-09-04-migration-plan.md"

#: The primitives that take a key. `notify` does not, because it asks nothing.
KEYED_PRIMITIVES: Set[str] = {"ask", "select", "confirm", "edit", "wait_for"}

#: What both a table placeholder and an f-string interpolation are reduced to before comparing,
#: because `service.<slug>.confirm` and `f"service.{service.name_slug}.confirm"` are one key.
PLACEHOLDER = "<*>"

#: Directories inside the package that ask nothing.
EXEMPT_DIRECTORIES: Set[str] = {"tests"}

#: Keys that must be found, one per module that asks something. Without them the walker could
#: match nothing at all and the test would still pass.
EXPECTED_KEYS: Set[str] = {
    "app.name",  # cli/commands/setup.py
    "env.name",  # cli/commands/deploy.py
    "env.region",  # cloud_providers/
    "envvar.<*>",  # deployment_strategies/monolithic.py
    "dockerfile.edit",  # service_detector.py
}


def _declared_keys() -> Set[str]:
    """
    Reads the interaction key table out of the migration plan.

    :return: Every declared key, with its placeholders normalised.
    """
    keys = set()
    for line in KEY_TABLE.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `"):
            continue
        first_column = line.split("|")[1]
        for key in re.findall(r"`([^`]+)`", first_column):
            keys.add(re.sub(r"<[^>]+>", PLACEHOLDER, key))
    return keys


def _key_of(argument: ast.expr) -> Optional[str]:
    """
    Reads a key out of the expression a call passes for it.

    :param argument: The first argument of an interaction call, or of a call to a helper that
        forwards one.
    :return: The key with every interpolation reduced to a placeholder, or None when the
        expression is neither a literal nor an f-string and so names nothing statically.
    """
    if isinstance(argument, ast.Constant):
        return argument.value

    if not isinstance(argument, ast.JoinedStr):
        return None

    parts = []
    for value in argument.values:
        if isinstance(value, ast.Constant):
            parts.append(value.value)
        else:
            parts.append(PLACEHOLDER)
    return "".join(parts)


def _is_an_interaction_call(node: ast.Call) -> bool:
    """
    Reports whether a call is one of the keyed primitives on an interaction.

    :param node: The call to inspect.
    :return: Whether it asks a person something.
    """
    if not isinstance(node.func, ast.Attribute) or node.func.attr not in KEYED_PRIMITIVES:
        return False

    # self.interact.ask(...), ctx.interact.select(...), state.context.interact.confirm(...)
    # and the plain `interact.ask(...)` a command binds first.
    receiver = node.func.value
    if isinstance(receiver, ast.Attribute):
        return receiver.attr == "interact"
    return isinstance(receiver, ast.Name) and receiver.id == "interact"


def _forwarders(tree: ast.AST) -> Tuple[Dict[str, int], Set[str]]:
    """
    Finds the helpers that take a key and pass it straight to an interaction.

    ``setup.py`` has one: a single editor loop, called once per document with the key of the
    document being reviewed. The literal is at the helper's call sites, so those are where the
    key has to be read from, and the call inside the helper has to be recognised as forwarding
    rather than treated as a key nothing can read.

    :param tree: The module to inspect.
    :return: The position of the key parameter by helper name, and the parameter names used.
    """
    positions: Dict[str, int] = {}
    parameter_names: Set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        parameters = [argument.arg for argument in node.args.args]
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or not _is_an_interaction_call(call):
                continue

            key_argument = call.args[0]
            if isinstance(key_argument, ast.Name) and key_argument.id in parameters:
                positions[node.name] = parameters.index(key_argument.id)
                parameter_names.add(key_argument.id)

    return positions, parameter_names


def _keys_used() -> Dict[str, List[str]]:
    """
    Walks the package for interaction calls, and for calls to the helpers that forward a key.

    :return: The keys each module asks, by module path.
    :raises AssertionError: A key is built in a way nothing can read statically, which would
        leave a question no flag, file or agent can answer.
    """
    trees = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT)
        if relative.parts[0] in EXEMPT_DIRECTORIES:
            continue
        trees[str(relative)] = ast.parse(path.read_text(encoding="utf-8"))

    positions: Dict[str, int] = {}
    forwarded_parameters: Set[str] = set()
    for tree in trees.values():
        module_positions, module_parameters = _forwarders(tree)
        positions.update(module_positions)
        forwarded_parameters |= module_parameters

    used: Dict[str, List[str]] = {}
    for module, tree in trees.items():
        keys = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            if _is_an_interaction_call(node):
                position = 0
            elif isinstance(node.func, ast.Name) and node.func.id in positions:
                position = positions[node.func.id]
            else:
                continue

            if position >= len(node.args):
                continue

            argument = node.args[position]
            key = _key_of(argument)
            if key is not None:
                keys.append(key)
                continue

            forwarding = isinstance(argument, ast.Name) and argument.id in forwarded_parameters
            assert forwarding, f"{module}: an interaction key must be a literal or an f-string"

        if keys:
            used[module] = keys
    return used


def test_every_key_asked_for_is_declared():
    """
    Collects the key from every interaction call in the package and fails on one the migration
    plan's table does not declare, so a new question cannot ship without a name a flag can use.
    """
    declared = _declared_keys()
    undeclared = {
        module: sorted(key for key in keys if key not in declared)
        for module, keys in _keys_used().items()
    }
    undeclared = {module: keys for module, keys in undeclared.items() if keys}

    assert not undeclared, (
        "Every interaction key must appear in the key table in"
        f" docs/notes/2026-09-04-migration-plan.md: {undeclared}"
    )


def test_the_walk_finds_the_questions_the_flows_ask():
    """
    Guards the walk itself: a collector that matched nothing would make the test above pass
    vacuously, so one key from each module that asks something has to turn up.
    """
    found = {key for keys in _keys_used().values() for key in keys}

    assert EXPECTED_KEYS <= found, f"missing: {sorted(EXPECTED_KEYS - found)}"
