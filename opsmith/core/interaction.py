"""The one way Opsmith asks a person something.

Core code never prompts. It calls the five primitives below, inline, wherever an answer is
needed, and the runtime decides whether that becomes a prompt on a terminal or an answer read
from a flag, a file or the answer store. Every call carries a stable key, because a question
that cannot be named cannot be answered by anything but a person.

The protocol is deliberately small and inline: a strategy neither declares its questions ahead
of time nor restructures its flow to use this. That is the whole contract third-party
strategies and cloud providers are asked to meet.
"""

from pathlib import Path
from typing import Any, Callable, List, Literal, Optional, Protocol

from pydantic import BaseModel, Field


class Choice(BaseModel):
    """One option offered by :meth:`Interaction.select`."""

    label: str = Field(
        ...,
        description=(
            "What to show for this option, in plain text. It carries no decoration: an"
            " implementation marks the recommended option however its medium does."
        ),
    )
    value: Any = Field(..., description="What select returns when this option is chosen.")
    recommended: bool = Field(
        False,
        description=(
            "Whether this is the option to offer first. At most one choice in a list should"
            " set it; it becomes the default when the caller does not name one."
        ),
    )


class Interaction(Protocol):
    """What a run asks a person, however that person is reached.

    Implementations live in ``opsmith/cli/``: :class:`~opsmith.cli.interaction.TerminalInteraction`
    prompts, and part 0e adds the headless one that resolves answers without a terminal.
    """

    def ask(
        self,
        key: str,
        message: str,
        *,
        default: Optional[str] = None,
        secret: bool = False,
        validate: Optional[Callable[[str], Optional[str]]] = None,
    ) -> str:
        """
        Asks for a value typed by hand.

        :param key: The stable key this answer is addressed by.
        :param message: The question, in plain text.
        :param default: What to use when the answer is left empty.
        :param secret: Whether the value must not be echoed, stored or logged.
        :param validate: Checks a candidate answer, returning the problem with it or None when
            it is acceptable. Note the shape: it returns the message rather than a bool, so the
            same rule reads the same way to a person and to a harness.
        :return: The answer.
        :raises InteractionCancelled: The person declined to answer.
        """
        ...

    def select(self, key: str, message: str, choices: List[Choice], *, default: Any = None) -> Any:
        """
        Asks for one option out of a list.

        :param key: The stable key this answer is addressed by.
        :param message: The question, in plain text.
        :param choices: The options to offer.
        :param default: The value to offer first. Falls back to the recommended choice.
        :return: The ``value`` of the chosen option, never its label.
        :raises InteractionCancelled: The person declined to answer.
        """
        ...

    def confirm(
        self, key: str, message: str, *, details: Any = None, default: bool = False
    ) -> bool:
        """
        Asks a yes or no question.

        :param key: The stable key this answer is addressed by.
        :param message: The question, in plain text.
        :param details: What the answer is about, for a reader that cannot see the terminal.
        :param default: The answer taken when the person just presses enter.
        :return: What they answered.
        :raises InteractionCancelled: The person declined to answer.
        """
        ...

    def edit(
        self,
        key: str,
        message: str,
        *,
        content: str,
        path: Optional[Path] = None,
        on_headless: Literal["accept", "fail"],
    ) -> str:
        """
        Hands a document to a person to edit.

        :param key: The stable key this answer is addressed by.
        :param message: What they are being asked to review, in plain text.
        :param content: The document as it stands. It is not read from ``path``, because the
            proposal being reviewed is usually not the file on disk yet.
        :param path: Where the content belongs once it is accepted, when it has a home. A
            headless run names it when it cannot edit for itself.
        :param on_headless: What a run with no person should do with this editor. A review
            editor ``accept``s the proposal; a fix editor for something that failed validation
            ``fail``s, because accepting it unchanged would only fail again.
        :return: The edited document.
        :raises InteractionCancelled: The person declined to edit.
        """
        ...

    def wait_for(
        self,
        key: str,
        message: str,
        *,
        check: Callable[[], bool],
        details: Any = None,
        timeout_s: Optional[int] = None,
    ) -> None:
        """
        Waits for something only a person can do, such as creating a DNS record.

        :param key: The stable key this wait is recorded under.
        :param message: What has to happen, in plain text.
        :param check: Reports whether it has happened yet.
        :param details: What has to be done, for a reader that cannot see the terminal.
        :param timeout_s: How long to wait before giving up, when the caller has an opinion.
        :raises InteractionCancelled: The person gave up waiting.
        """
        ...

    def notify(self, message: str, *, details: Any = None) -> None:
        """
        Tells the person something, without asking for anything.

        :param message: What they should know, in plain text.
        :param details: Machine-readable context for a harness reading the run.
        """
        ...
