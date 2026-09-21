"""Every interaction key used in the code is one the migration plan declares.

Keys are the whole point of the interaction API: a flag, an answers file or a driving agent can
only supply an answer for a question it can name. So the names cannot be invented at the call
site - they come from one table, and this walks the package to prove it.

There are two ways a module names a key: by calling one of the primitives on an interaction, and
by declaring a :class:`~opsmith.core.questions.Question`. Both are walked, because a declared
question is answered by exactly the same flag as an asked one - a provider that stopped calling
``interact`` directly has not stopped asking.
"""

import ast
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
KEY_TABLE = PACKAGE_ROOT.parent / "docs" / "notes" / "2026-09-04-migration-plan.md"

#: The primitives that take a key. `notify` does not, because it asks nothing.
KEYED_PRIMITIVES: Set[str] = {"ask", "select", "confirm", "edit", "wait_for"}

#: The constructor that declares a question rather than asking it. Its key is the first thing it
#: is given, by keyword, because a declaration is read by people more often than it is written.
QUESTION_CONSTRUCTOR = "Question"

#: What both a table placeholder and an f-string interpolation are reduced to before comparing,
#: because `service.<slug>.confirm` and `f"service.{service.name_slug}.confirm"` are one key.
PLACEHOLDER = "<*>"

#: Directories inside the package that ask nothing.
EXEMPT_DIRECTORIES: Set[str] = {"tests"}

#: The keys that are both declared as data and asked by hand in the same module, and which
#: module does both. Everywhere else a declaration is handed to ``ask_all``, so the declaration
#: *is* the call site and there is nothing for it to drift from; these are the only places the
#: two can disagree, which is what the test below exists to stop.
DECLARED_AND_ASKED_BY_HAND: Dict[str, Set[str]] = {
    "core/operations.py": {"env.cloud_provider", "env.name", "env.strategy"},
    "deployment_strategies/monolithic.py": {
        "env.instance_type",
        "envvar.<*>",
        "build_env.<*>.<*>",
    },
}

#: The one module that asks a question it does not name. ``core/questions.py`` walks a list of
#: declarations and asks each one, so the key it passes is whatever it was handed - and every key
#: it can ever pass is a ``Question`` this walk has already read from wherever it was declared.
GENERIC_ASKERS: Set[str] = {"core/questions.py"}

#: Keys that must be found, one per module that asks something. Without them the walker could
#: match nothing at all and the test would still pass.
EXPECTED_KEYS: Set[str] = {
    "app.name",  # cli/commands/setup.py
    "env.name",  # cli/commands/deploy.py
    "env.region",  # cloud_providers/, declared rather than asked
    "env.domain.<*>",  # core/operations.py, declared rather than asked
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
        # A constant that is not a string names no key - a number or None in this position is
        # not a key spelled oddly, it is not a key at all.
        return argument.value if isinstance(argument.value, str) else None

    if not isinstance(argument, ast.JoinedStr):
        return None

    parts: List[str] = []
    for value in argument.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            parts.append(PLACEHOLDER)
    return "".join(parts)


def _is_a_question(node: ast.Call) -> bool:
    """
    Reports whether a call declares a question.

    :param node: The call to inspect.
    :return: Whether it is a ``Question(...)`` constructor.
    """
    if isinstance(node.func, ast.Name):
        return node.func.id == QUESTION_CONSTRUCTOR
    return isinstance(node.func, ast.Attribute) and node.func.attr == QUESTION_CONSTRUCTOR


def _declared_key(node: ast.Call) -> Optional[str]:
    """
    Reads the key out of a question declaration.

    A declaration writes its placeholder out in full - ``env.domain.<slug>`` - where an asked key
    interpolates one, so it is normalised here rather than by :func:`_key_of`.

    :param node: The ``Question(...)`` call.
    :return: The key with its placeholders normalised, or None when it is not named statically.
    """
    named = None
    for keyword in node.keywords:
        if keyword.arg == "key":
            named = _key_of(keyword.value)
    if named is None and node.args:
        named = _key_of(node.args[0])

    return None if named is None else re.sub(r"<[^>]+>", PLACEHOLDER, named)


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


def _assigned_keys(tree: ast.AST) -> Dict[str, str]:
    """
    Finds the local variables a module builds a key into before asking with it.

    ``_prompt_for_build_env_vars`` does this: it needs the key three times - to migrate an older
    value, to read the remembered one and to ask - so it names it once. Without this the walker
    would see a bare ``key`` and skip it, and a question would go unchecked.

    A name assigned more than once is left out rather than guessed at.

    :param tree: The module to inspect.
    :return: The key each such variable holds, with its interpolations normalised.
    """
    found: Dict[str, List[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue

        key = _key_of(node.value)
        if key is not None:
            found.setdefault(target.id, []).append(key)

    return {name: keys[0] for name, keys in found.items() if len(set(keys)) == 1}


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


def _keys_used(*, declarations: bool = True, asks: bool = True) -> Dict[str, List[str]]:
    """
    Walks the package for interaction calls, declared questions, and the helpers that forward a
    key.

    :param declarations: Whether to collect the key of each ``Question(...)`` declaration.
    :param asks: Whether to collect the key of each call to an interaction primitive.
    :return: The keys each module names, by module path.
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
        if module.replace("\\", "/") in GENERIC_ASKERS:
            continue

        assigned = _assigned_keys(tree)
        keys = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            if _is_a_question(node):
                declared = _declared_key(node)
                assert declared is not None, f"{module}: a declared question must name its key"
                if declarations:
                    keys.append(declared)
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
            if key is None and isinstance(argument, ast.Name):
                key = assigned.get(argument.id)
            if key is not None:
                if asks:
                    keys.append(key)
                continue

            forwarding = isinstance(argument, ast.Name) and argument.id in forwarded_parameters
            assert forwarding, f"{module}: an interaction key must be a literal or an f-string"

        if keys:
            used[module] = keys
    return used


def test_every_key_asked_for_is_declared():
    """
    Collects the key from every interaction call and every declared question in the package and
    fails on one the migration plan's table does not declare, so a new question cannot ship
    without a name a flag can use.
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


def test_a_declaration_that_is_also_asked_by_hand_agrees_with_its_call_site():
    """
    Guards the one duplication declaring questions introduces.

    A key that is declared for ``env plan`` and asked separately by hand has two spellings of the
    same name, and renaming one of them would leave the plan quietly promising a question the run
    never asks. Every such pair is named above, and both halves have to be there.
    """
    declared = _keys_used(asks=False)
    asked = _keys_used(declarations=False)

    for module, keys in DECLARED_AND_ASKED_BY_HAND.items():
        assert keys <= set(declared.get(module, [])), f"{module} no longer declares all of {keys}"
        assert keys <= set(asked.get(module, [])), f"{module} no longer asks all of {keys}"
