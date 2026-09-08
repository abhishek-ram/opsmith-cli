"""The commands, and the way one declares what it needs before it runs.

The declaration lives here rather than in ``opsmith/cli/app.py`` because that module imports these
command modules; a decorator defined there could not be imported back.
"""

from typing import Callable, Tuple, TypeVar

#: Where :func:`requires` records a command's external tools, read by ``handle_errors``.
REQUIREMENTS_ATTRIBUTE = "__opsmith_requires__"

Command = TypeVar("Command", bound=Callable)


def requires(*tools: str) -> Callable[[Command], Command]:
    """
    Declares the external command line tools that must work before this command's body runs.

    A command that declares nothing runs anywhere, which is what lets ``config validate`` check a
    repository on a machine with neither docker nor terraform installed.

    :param tools: The command names the command needs, such as "docker" and "terraform".
    :return: A decorator that tags the command function and returns it unchanged, so Typer still
        sees the original signature.
    """

    def decorate(func: Command) -> Command:
        setattr(func, REQUIREMENTS_ATTRIBUTE, tuple(tools))
        return func

    return decorate


def requirements_of(func: Callable) -> Tuple[str, ...]:
    """
    Returns the tools a command declared, or an empty tuple if it declared none.

    :param func: The command function.
    :return: The declared tool names.
    """
    return getattr(func, REQUIREMENTS_ATTRIBUTE, ())
