"""The one way Opsmith asks a person something.

Core code never prompts. It calls the five primitives below, inline, wherever an answer is
needed, and the runtime decides whether that becomes a prompt on a terminal or an answer read
from a flag, a file or the answer store. Every call carries a stable key, because a question
that cannot be named cannot be answered by anything but a person.

The protocol is deliberately small and inline: a strategy neither declares its questions ahead
of time nor restructures its flow to use this. That is the whole contract third-party
strategies and cloud providers are asked to meet.

Both implementations of it live here in spirit, though only one is here in fact: the terminal one
needs a prompt library and so belongs in ``opsmith/cli/``, while the headless one needs nothing but
the answers it was given and so belongs with the protocol.
"""

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Protocol, Set, Tuple

from pydantic import BaseModel, Field

from opsmith.core.answers import (
    DESTRUCTIVE_KEYS,
    AnswerSources,
    AnswerStore,
    environment_variable_for,
)
from opsmith.core.errors import (
    InvalidArgument,
    InvalidConfig,
    ManualEditRequired,
    MissingAnswerError,
    OpsmithError,
    PendingActionError,
)
from opsmith.core.events import STEP_INTERACT, EventSink


class Notice(BaseModel):
    """One thing a run told the user through :meth:`Interaction.notify`.

    It lives here rather than in ``core/results.py`` because ``notify`` is what produces it, and
    because results imports ``opsmith.types``, which reaches ``cloud_providers`` and back to this
    module - an import this way round closes that circle, the other way round does not.
    """

    message: str = Field(..., description="What the run said, in plain text.")
    details: Optional[Any] = Field(
        None, description="Machine-readable context the notice carried, when it carried any."
    )


def next_steps_of(details: Any) -> List[str]:
    """
    Reads the next steps a notice carried, however it carried them.

    A caller may attach one instruction or several, so both are accepted and flattened: the result
    envelope holds a flat list, not a list of lists.

    :param details: Whatever the caller attached to the notice.
    :return: The next steps it named, which is usually none.
    """
    if not isinstance(details, dict):
        return []

    declared = details.get("next_steps")
    if declared is None:
        return []
    if isinstance(declared, str):
        return [declared]
    if isinstance(declared, (list, tuple)):
        return [str(step) for step in declared]
    return [str(declared)]


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

    There are two implementations. :class:`~opsmith.cli.interaction.TerminalInteraction` prompts;
    :class:`HeadlessInteraction`, below, resolves an answer from what the run was told up front and
    stops the run when nothing has it.

    Both collect what they were asked to :meth:`notify`, because a command's result reports it and
    a command does not know which implementation it is talking to.
    """

    #: Everything the run told the user, in the order it told them.
    notices: List[Notice]

    #: What the run asked the user or the driver to do next.
    next_steps: List[str]

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


#: Field names on :class:`~opsmith.core.events.Event`. Details are handed to the sink as keyword
#: arguments, so a detail sharing one of these names would collide with the event's own.
EVENT_FIELDS = {"kind", "step", "message", "data"}


def event_data(details: Any) -> Dict:
    """
    Shapes an interaction's details into the data an event carries.

    A mapping is spread across the event's data so a reader sees the fields by name, except when
    one of them is named after a field of the event itself - a ``ConfigIssue`` has a ``message``,
    and spreading that would pass ``message`` twice. Those are nested whole instead, which is what
    a non-mapping gets anyway.

    :param details: Whatever the caller attached to the interaction.
    :return: Keyword data for the event, empty when there is nothing to attach.
    """
    if details is None:
        return {}
    if isinstance(details, dict) and not EVENT_FIELDS.intersection(details):
        return details
    return {"details": details}


def choice_token(choice: Choice) -> Any:
    """
    Returns the form of an option that can be written down and handed back.

    A choice's value may be any object - an instance type is a whole model - while a flag, a YAML
    file and an error envelope can only carry text. So an option is written down as its value when
    that is already a scalar, as the name the value carries when it has one, and as its label
    otherwise. Whatever comes back, :func:`match_choice` finds the option again.

    :param choice: The option.
    :return: How to refer to it.
    """
    if choice.value is None or isinstance(choice.value, (str, int, float, bool)):
        return choice.value

    name = getattr(choice.value, "name", None)
    if isinstance(name, str) and name:
        return name

    return choice.label


def match_choice(choices: List[Choice], supplied: Any) -> Optional[Choice]:
    """
    Finds the choice an answer names.

    :param choices: The options that were offered.
    :param supplied: What the answer said, from a flag, a file or the store.
    :return: The choice it names, or None when it names none of them.
    """
    for choice in choices:
        if supplied is choice.value or supplied == choice.value:
            return choice

    text = str(supplied)
    for choice in choices:
        if text in (str(choice_token(choice)), str(choice.value), choice.label):
            return choice
    return None


def describe_choices(choices: List[Choice]) -> List[Dict[str, Any]]:
    """
    Describes the options for a reader that cannot see the terminal.

    :param choices: The options that were offered.
    :return: One label and value mapping per option, each value in the form it can be sent back.
    """
    return [{"label": choice.label, "value": choice_token(choice)} for choice in choices]


def storable(choice: Choice) -> Any:
    """
    Returns the form of a chosen option that can be written to the answer store.

    :param choice: The option that was chosen.
    :return: How to refer to it, which the next run matches back to the same option.
    """
    return choice_token(choice)


def as_boolean(value: Any) -> Tuple[Optional[bool], Optional[str]]:
    """
    Reads a yes or no answer that may have arrived as text.

    :param value: What was supplied.
    :return: The answer, and the problem with it when it is not one.
    """
    if isinstance(value, bool):
        return value, None
    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "1", "on"}:
        return True, None
    if text in {"false", "no", "n", "0", "off"}:
        return False, None
    return None, f"'{value}' is not a yes or no answer"


class HeadlessInteraction:
    """Answers questions without a person, and stops the run when it cannot.

    Every answer comes from somewhere the caller named up front - a flag, a file, an environment
    variable - or from what this environment has already been asked. When none of them has it, the
    run stops with the key, the shape of the answer and the command to run again, which is the
    whole of the driver loop: run; add the one missing answer; run again.
    """

    def __init__(
        self,
        sources: AnswerSources,
        answers: AnswerStore,
        *,
        events: EventSink,
        wait_timeout: int = 600,
        resume: str = "",
        poll_interval_s: int = 10,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        """
        :param sources: What the run was told up front.
        :param answers: What this environment has already answered, and where a new answer goes.
        :param events: Where progress is reported.
        :param wait_timeout: Seconds to poll an external action before giving up.
        :param resume: The command to run again, reported with every stop.
        :param poll_interval_s: Seconds between two checks of a wait.
        :param sleep: How to wait between checks. Injected so a test can prove a ten minute
            timeout without taking ten minutes.
        :param monotonic: How to read the clock, injected for the same reason.
        """
        self.sources = sources
        self.answers = answers
        self.events = events
        self.wait_timeout = wait_timeout
        self.resume = resume
        self.poll_interval_s = poll_interval_s
        self.sleep = sleep
        self.monotonic = monotonic

        #: What the run told the user, for the result envelope the command builds.
        self.notices: List[Notice] = []
        self.next_steps: List[str] = []

        #: Review editors already accepted. A second call for one means the document it accepted
        #: did not validate, and accepting it again would loop forever.
        self._accepted_edits: Set[str] = set()

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
        Resolves a value that would have been typed by hand.

        :param key: The stable key this answer is addressed by.
        :param message: The question, in plain text.
        :param default: What to use under ``--accept-defaults``.
        :param secret: Whether the value must not be echoed, stored in the clear or logged.
        :param validate: Checks a candidate answer, returning the problem with it or None.
        :return: The answer.
        :raises MissingAnswerError: Nothing supplied one.
        :raises InvalidArgument: The supplied answer is one the question refuses.
        """

        def accept(raw: Any) -> Tuple[Any, Optional[str]]:
            text = "" if raw is None else str(raw)
            return text, validate(text) if validate is not None else None

        found, answer = self._resolve(key, accept=accept, default=default, secret=secret)
        if not found:
            raise self._missing(key, message, primitive="ask", default=default, secret=secret)

        self.answers.record(key, answer, secret=secret)
        return answer

    def select(self, key: str, message: str, choices: List[Choice], *, default: Any = None) -> Any:
        """
        Resolves one option out of a list, by its value or by its label.

        :param key: The stable key this answer is addressed by.
        :param message: The question, in plain text.
        :param choices: The options to choose between.
        :param default: The option to take under ``--accept-defaults``. Falls back to the
            recommended one; an arbitrary first option is never chosen for the caller.
        :return: The ``value`` of the chosen option.
        :raises MissingAnswerError: Nothing supplied one.
        :raises InvalidArgument: The supplied answer names none of the options.
        """

        def accept(raw: Any) -> Tuple[Any, Optional[str]]:
            choice = match_choice(choices, raw)
            if choice is None:
                return None, f"'{raw}' is not one of the options offered"
            return choice, None

        fallback = default
        if fallback is None:
            recommended = [choice for choice in choices if choice.recommended]
            fallback = recommended[0].value if recommended else None

        found, chosen = self._resolve(key, accept=accept, default=fallback, choices=choices)
        if not found:
            raise self._missing(key, message, primitive="select", choices=choices, default=fallback)

        self.answers.record(key, storable(chosen))
        return chosen.value

    def confirm(
        self, key: str, message: str, *, details: Any = None, default: bool = False
    ) -> bool:
        """
        Resolves a yes or no question.

        A key that gates something destructive has to be answered by name - there is no blanket
        flag that covers it - and its default is never taken, however the run was invoked.

        :param key: The stable key this answer is addressed by.
        :param message: The question, in plain text.
        :param details: What the answer is about, reported when the run stops here.
        :param default: The answer to take under ``--accept-defaults``.
        :return: What was answered.
        :raises MissingAnswerError: Nothing supplied one.
        """
        found, answer = self._resolve(key, accept=as_boolean, default=default)
        if not found:
            raise self._missing(key, message, primitive="confirm", default=default, details=details)

        self.answers.record(key, answer)
        return answer

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
        Decides what to do with an editor nobody can open.

        :param key: The stable key this answer is addressed by.
        :param message: What a person would have been asked to review.
        :param content: The document as it stands.
        :param path: Where the document belongs. A fix editor writes the proposal there, so the
            file the error names is the file to edit.
        :param on_headless: What this editor does with no person: ``accept`` the proposal, or
            ``fail`` because accepting something that did not validate would only fail again.
        :return: The document, unchanged, for a review editor.
        :raises ManualEditRequired: A fix editor, which nobody can fix.
        :raises InvalidConfig: A review editor was asked twice, so what it accepted is not valid.
        """
        if on_headless == "accept":
            if key in self._accepted_edits:
                raise InvalidConfig(
                    f"The document at '{key}' was accepted unchanged and is still not valid.",
                    hint=(
                        "Run the command again on a terminal to edit it, or fix what the warnings"
                        " above describe."
                    ),
                    details={"key": key},
                )
            self._accepted_edits.add(key)
            return content

        if path is None:
            raise OpsmithError(
                f"The editor at '{key}' has nowhere to write what it could not fix.",
                hint="This is a bug in opsmith: a fix editor must name the file it is fixing.",
                details={"key": key},
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        raise ManualEditRequired(key, message, path=str(path), resume=self.resume)

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
        Polls until something a person had to do has been done, or until the run gives up.

        :param key: The stable key this wait is recorded under.
        :param message: What has to happen, in plain text.
        :param check: Reports whether it has happened yet.
        :param details: What has to be done, reported when the run stops here.
        :param timeout_s: The caller's own limit, when it has an opinion. The run-wide
            ``--wait-timeout`` is the ceiling either way.
        :raises PendingActionError: It had not happened by the time the run gave up.
        """
        if self.answers.get(key) is True:
            return

        limit = self.wait_timeout if timeout_s is None else min(timeout_s, self.wait_timeout)
        started = self.monotonic()
        announced = False

        while True:
            if check():
                self.answers.record(key, True)
                return

            waited = int(self.monotonic() - started)
            if waited >= limit:
                raise PendingActionError(
                    key,
                    message,
                    details=details,
                    waited_s=waited,
                    timeout_s=limit,
                    resume=self.resume,
                )

            if not announced:
                self.notify(message, details=details)
                announced = True

            self.sleep(min(self.poll_interval_s, max(limit - waited, 1)))

    def notify(self, message: str, *, details: Any = None) -> None:
        """
        Reports something the run wants known, and keeps it for the result envelope.

        :param message: What should be known, in plain text.
        :param details: Machine-readable context for a harness reading the run.
        """
        self.events.log(STEP_INTERACT, message, **event_data(details))
        self.notices.append(Notice(message=message, details=details))
        self.next_steps.extend(next_steps_of(details))

    def _resolve(
        self,
        key: str,
        *,
        accept: Callable[[Any], Tuple[Any, Optional[str]]],
        default: Any = None,
        secret: bool = False,
        choices: Optional[List[Choice]] = None,
    ) -> Tuple[bool, Any]:
        """
        Finds the answer to one question, in the order the sources are consulted.

        A value the caller supplied and the question refuses stops the run as a usage error rather
        than as a missing answer, because reporting it as missing would tell a driver to supply
        something it already supplied. A value read back from the store that no longer fits is a
        different matter - the store is Opsmith's own, so a stale entry is dropped and the search
        carries on rather than wedging the run.

        :param key: The interaction key.
        :param accept: Turns a raw value into the answer, or into the problem with it.
        :param default: What the question would fall back to under ``--accept-defaults``.
        :param secret: Whether the value may be repeated in an error.
        :param choices: The options offered, for the error that reports a refused answer.
        :return: Whether an answer was found, and the answer.
        :raises InvalidArgument: A supplied answer is one the question refuses.
        """
        found, raw, source = self.sources.supplied(key)
        if found:
            answer, problem = accept(raw)
            if problem:
                raise InvalidArgument(
                    f"The answer given for '{key}' cannot be used: {problem}.",
                    hint=f"Correct the value supplied through {source} and run again.",
                    details={
                        "key": key,
                        "source": source,
                        "problem": problem,
                        "choices": describe_choices(choices) if choices else [],
                    },
                )
            return True, answer

        if self.answers.has(key):
            answer, problem = accept(self.answers.get(key))
            if problem is None:
                return True, answer
            self.events.warning(
                STEP_INTERACT,
                f"Ignoring the stored answer for '{key}': {problem}.",
                key=key,
            )

        takes_default = self.sources.accept_defaults and key not in DESTRUCTIVE_KEYS
        if takes_default and default is not None:
            answer, problem = accept(default)
            if problem is None:
                return True, answer

        return False, None

    def _missing(
        self,
        key: str,
        message: str,
        *,
        primitive: str,
        choices: Optional[List[Choice]] = None,
        default: Any = None,
        secret: bool = False,
        details: Any = None,
    ) -> MissingAnswerError:
        """
        Builds the stop that tells a driver exactly what to supply.

        :param key: The interaction key that went unanswered.
        :param message: The question as it was worded.
        :param primitive: Which primitive asked.
        :param choices: The options offered, when there were any.
        :param default: What the question would have taken under ``--accept-defaults``.
        :param secret: Whether the answer must not go on a command line.
        :param details: What the question was about, for a confirmation.
        :return: The error to raise.
        """
        described_default = default
        if choices and default is not None:
            chosen = match_choice(choices, default)
            described_default = storable(chosen) if chosen else None

        error = MissingAnswerError(
            key,
            message,
            primitive=primitive,
            choices=describe_choices(choices) if choices else None,
            default=described_default,
            secret=secret,
            env_var=environment_variable_for(key),
            resume=self.resume,
        )
        if isinstance(details, dict):
            error.details.update(details)
        return error
