"""The commands, and the way one declares what it needs before it runs.

The declaration lives here rather than in ``opsmith/cli/app.py`` because that module imports these
command modules; a decorator defined there could not be imported back.
"""

from typing import Callable, Tuple, TypeVar

#: Where :func:`requires` records a command's external tools, read by ``handle_errors``.
REQUIREMENTS_ATTRIBUTE = "__opsmith_requires__"

#: Where :func:`no_model` records that a command needs no model, read by ``handle_errors``.
NO_MODEL_ATTRIBUTE = "__opsmith_no_model__"

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


def no_model(func: Command) -> Command:
    """
    Declares that this command does not use the model, so it must not insist on one.

    A model is a requirement of Opsmith and almost every command is nothing without it, which is
    why ``handle_errors`` resolves one before every command body. ``agent install`` is the
    exception that proves the rule: it copies files into a harness's skill directory, it is the
    first command a new user runs, and demanding an API key to do it would be absurd.

    The same sentence as :func:`requires`, one step along: a command that declares no tools is
    never probed for any, and a command that declares no model is never asked for one.

    :param func: The command function.
    :return: The same function, tagged, so Typer still sees the original signature.
    """
    setattr(func, NO_MODEL_ATTRIBUTE, True)
    return func


def needs_model(func: Callable) -> bool:
    """
    :param func: The command function.
    :return: Whether a model has to be configured before this command's body runs.
    """
    return not getattr(func, NO_MODEL_ATTRIBUTE, False)
