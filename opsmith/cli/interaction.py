"""Asking a person something, when the person is at a terminal.

This is the only place in Opsmith that draws a prompt. Core code calls the primitives on
:class:`~opsmith.core.interaction.Interaction`; this maps each of them onto the ``inquirer``
question that Opsmith has always used, so the flow a user sees is unchanged.
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

import inquirer
from inquirer import errors as inquirer_errors

from opsmith.cli.output import BaseRenderer
from opsmith.core.errors import InteractionCancelled
from opsmith.core.events import STEP_INTERACT
from opsmith.core.interaction import Choice

#: Appended to the label of the choice the model, or the code, recommends.
RECOMMENDED_SUFFIX = " (Recommended)"


def _adapt_validator(
    validate: Optional[Callable[[str], Optional[str]]],
) -> Any:
    """
    Turns an interaction validator into the callable inquirer expects.

    An interaction validator takes a value and returns what is wrong with it. Inquirer calls
    ``(answers, value)`` and wants a truthy result, reporting the problem only if the validator
    raises. This bridges the two, so the message the core wrote is the message the user reads.

    :param validate: The interaction validator, or None when the answer is unconstrained.
    :return: What to pass as inquirer's ``validate``, which is ``True`` when there is no rule.
    """
    if validate is None:
        return True

    def inquirer_validate(_: Dict, value: str) -> bool:
        problem = validate(value)
        if problem:
            raise inquirer_errors.ValidationError(value, reason=problem)
        return True

    return inquirer_validate


def _event_data(details: Any) -> Dict:
    """
    Shapes an interaction's details into the data an event carries.

    :param details: Whatever the caller attached to the interaction.
    :return: Keyword data for the event, empty when there is nothing to attach.
    """
    if details is None:
        return {}
    if isinstance(details, dict):
        return details
    return {"details": details}


class TerminalInteraction:
    """Asks through ``inquirer``, on the terminal the run was started from."""

    def __init__(self, renderer: BaseRenderer):
        """
        :param renderer: The run's renderer. It is asked to clear anything it is animating
            before a prompt is drawn, and it is where :meth:`notify` reports.
        """
        self.renderer = renderer

    def _prompt(self, key: str, question: Any) -> Any:
        """
        Puts one question to the user and returns what they answered.

        :param key: The interaction key, which is also the question's name.
        :param question: The inquirer question to render.
        :return: The answer.
        :raises InteractionCancelled: The user interrupted the prompt.
        """
        # A spinner and a prompt drawing on the same terminal garble each other, and a slow
        # step often ends by asking about what it found.
        self.renderer.stop_waiting()

        try:
            answers = inquirer.prompt([question], raise_keyboard_interrupt=True)
        except KeyboardInterrupt as err:
            raise InteractionCancelled(key, question.message) from err

        if answers is None:
            raise InteractionCancelled(key, question.message)

        return answers[key]

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
        :param secret: Whether to hide what is typed.
        :param validate: Checks a candidate answer, returning the problem with it or None.
        :return: The answer.
        :raises InteractionCancelled: The user interrupted the prompt.
        """
        question_class = inquirer.Password if secret else inquirer.Text
        question = question_class(
            key,
            message=message,
            default=default,
            validate=_adapt_validator(validate),
        )
        return self._prompt(key, question)

    def select(self, key: str, message: str, choices: List[Choice], *, default: Any = None) -> Any:
        """
        Asks for one option out of a list.

        :param key: The stable key this answer is addressed by.
        :param message: The question, in plain text.
        :param choices: The options to offer.
        :param default: The value to offer first. Falls back to the recommended choice.
        :return: The value of the chosen option.
        :raises InteractionCancelled: The user interrupted the prompt.
        """
        options = []
        selected = default
        for choice in choices:
            label = choice.label + RECOMMENDED_SUFFIX if choice.recommended else choice.label
            options.append((label, choice.value))
            if choice.recommended and selected is None:
                selected = choice.value

        question = inquirer.List(key, message=message, choices=options, default=selected)
        return self._prompt(key, question)

    def confirm(
        self, key: str, message: str, *, details: Any = None, default: bool = False
    ) -> bool:
        """
        Asks a yes or no question.

        :param key: The stable key this answer is addressed by.
        :param message: The question, in plain text.
        :param details: What the answer is about. A user at a terminal has already been shown
            it, so only a headless run reads this.
        :param default: The answer taken when the user just presses enter.
        :return: What they answered.
        :raises InteractionCancelled: The user interrupted the prompt.
        """
        question = inquirer.Confirm(key, message=message, default=default)
        return self._prompt(key, question)

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
        Opens the user's editor on a document and returns what they saved.

        :param key: The stable key this answer is addressed by.
        :param message: What they are being asked to review, in plain text.
        :param content: The document as it stands, which seeds the editor.
        :param path: Where the content belongs once accepted. Unused here; part 0e names it
            when it cannot open an editor.
        :param on_headless: What a run with no person should do. Unused here, for the same
            reason.
        :return: The edited document.
        :raises InteractionCancelled: The user interrupted the prompt.
        """
        question = inquirer.Editor(key, message=message, default=content)
        return self._prompt(key, question)

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
        Waits for something only the user can do, re-checking whenever they say to.

        There is no timeout on a terminal: a person is sitting there, and they decide when to
        stop waiting. ``timeout_s`` is for the headless implementation.

        :param key: The stable key this wait is recorded under.
        :param message: What has to happen, in plain text.
        :param check: Reports whether it has happened yet.
        :param details: What has to be done, for a harness reading the run.
        :param timeout_s: Ignored here.
        :raises InteractionCancelled: The user stopped waiting.
        """
        while not check():
            self.notify(message, details=details)
            if not self.confirm(key, "Check again?", details=details, default=True):
                raise InteractionCancelled(key, message)

    def notify(self, message: str, *, details: Any = None) -> None:
        """
        Tells the user something, without asking for anything.

        It goes through the renderer rather than to the terminal directly, so it is styled like
        every other line in text mode and streamed like every other event in JSON mode.

        :param message: What they should know, in plain text.
        :param details: Machine-readable context for a harness reading the run.
        """
        self.renderer.log(STEP_INTERACT, message, **_event_data(details))
