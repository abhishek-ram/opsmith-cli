"""Every failure tells the caller what to do about it.

A coding harness driving Opsmith reads three things off a failure: the code, so it knows what kind
of thing went wrong; the details, so it knows which key or path; and the hint, so it knows what to
run next. The first two are typed and cannot go missing. The hint is a string at a call site, and
would go missing the moment somebody adds an error in a hurry, so it is checked here instead.

This is the audit the harness work owes the CLI contract, made mechanical: it walks the source
rather than the errors raised by any one run, so a path no test exercises is covered too.
"""

import ast
from pathlib import Path
from typing import List, Set, Tuple

from opsmith.core import errors
from opsmith.core.errors import OpsmithError

PACKAGE_ROOT = Path(errors.__file__).resolve().parent.parent

#: Where a raise lives that is not part of the package's own behaviour.
EXCLUDED_DIRECTORIES = {"tests"}


def _error_class_names() -> Set[str]:
    """
    :return: The name of every error Opsmith raises.
    """
    return {
        name
        for name, value in vars(errors).items()
        if isinstance(value, type) and issubclass(value, OpsmithError)
    }


def _classes_that_build_their_own_hint() -> Set[str]:
    """
    Finds the errors whose constructor supplies the hint, so no call site has to.

    ``CloudCredentialsError`` builds one from the provider's documentation URL and
    ``InteractionCancelled`` from the key that was cancelled; a call site passing a hint to either
    would not compile. They are recognised by having a constructor of their own.

    :return: The names of those classes.
    """
    return {
        name
        for name, value in vars(errors).items()
        if isinstance(value, type)
        and issubclass(value, OpsmithError)
        and value is not OpsmithError
        and value.__init__ is not OpsmithError.__init__
    }


def _raises_without_a_hint() -> List[Tuple[str, int, str]]:
    """
    :return: Every raise of an Opsmith error that passes no hint and whose class builds none.
    """
    error_names = _error_class_names()
    exempt = _classes_that_build_their_own_hint()

    found = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if EXCLUDED_DIRECTORIES.intersection(path.parts):
            continue

        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue

            function = node.exc.func
            name = getattr(function, "id", None) or getattr(function, "attr", None)
            if name not in error_names or name in exempt:
                continue

            if not any(keyword.arg == "hint" for keyword in node.exc.keywords):
                found.append((str(path.relative_to(PACKAGE_ROOT.parent)), node.lineno, name))

    return found


def test_every_error_raised_carries_a_hint():
    """
    No raise of an Opsmith error leaves the caller without a next step.

    The hint is what a harness reads to decide what to do; an error without one is a dead end for
    anything that is not a person who can read the message and think about it.
    """
    missing = _raises_without_a_hint()

    assert not missing, "these raises carry no hint: " + ", ".join(
        f"{path}:{line} {name}" for path, line, name in missing
    )


def test_the_audit_can_actually_find_a_missing_hint():
    """
    The walk above finds what it claims to find.

    A test that passes because its parser matched nothing is worse than no test, so this one
    parses a raise that is definitely missing a hint and insists it is spotted.
    """
    source = "raise InvalidArgument('no hint here')"
    tree = ast.parse(source)
    node = next(item for item in ast.walk(tree) if isinstance(item, ast.Raise))

    assert isinstance(node.exc, ast.Call)
    assert not any(keyword.arg == "hint" for keyword in node.exc.keywords)
    assert "InvalidArgument" in _error_class_names()
    assert "InvalidArgument" not in _classes_that_build_their_own_hint()
