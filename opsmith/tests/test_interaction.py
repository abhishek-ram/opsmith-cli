"""Tests for the terminal interaction: how each primitive becomes an inquirer question."""

from pathlib import Path
from unittest.mock import patch

import inquirer
import pytest
from inquirer import errors as inquirer_errors

from opsmith.cli.interaction import TerminalInteraction
from opsmith.core.errors import EXIT_CODES, InteractionCancelled
from opsmith.core.interaction import Choice
from opsmith.tests.conftest import RecordingSink


class FakeRenderer(RecordingSink):
    """A renderer that records events and counts how often it was asked to clear the spinner."""

    def __init__(self):
        super().__init__()
        self.stop_waiting_calls = 0

    def stop_waiting(self):
        """Records that a prompt asked for the terminal to itself."""
        self.stop_waiting_calls += 1


class PromptRecorder:
    """Stands in for ``inquirer.prompt``, recording questions and answering them as scripted."""

    def __init__(self, *answers):
        """
        :param answers: What to answer, in the order the questions are asked. The last answer is
            repeated once the list runs out, so a re-asking loop does not have to be counted out.
        """
        self.answers = list(answers)
        self.questions = []

    def __call__(self, questions, raise_keyboard_interrupt=False):
        """
        :param questions: The one-question list the interaction built.
        :param raise_keyboard_interrupt: Asserted, because the cancel path depends on it.
        :return: The answer, keyed by the question's name as inquirer keys it.
        """
        assert raise_keyboard_interrupt is True
        assert len(questions) == 1
        question = questions[0]
        self.questions.append(question)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return {question.name: answer}

    @property
    def question(self):
        """:return: The only question asked, failing when more than one was."""
        assert len(self.questions) == 1
        return self.questions[0]


@pytest.fixture
def renderer() -> FakeRenderer:
    """A renderer that records what the interaction reports."""
    return FakeRenderer()


@pytest.fixture
def interact(renderer: FakeRenderer) -> TerminalInteraction:
    """The terminal interaction, reporting into the recording renderer."""
    return TerminalInteraction(renderer)


def prompting(*answers) -> PromptRecorder:
    """
    Patches inquirer for the duration of a test.

    :param answers: What each prompt should answer.
    :return: The recorder, to read the questions off afterwards.
    """
    return PromptRecorder(*answers)


def test_ask_builds_a_text_question(interact):
    """ask() asks an inquirer Text carrying the key, the message and the default."""
    recorder = prompting("acme")
    with patch("opsmith.cli.interaction.inquirer.prompt", recorder):
        answer = interact.ask("app.name", "Enter the application name", default="default-app")

    assert answer == "acme"
    assert isinstance(recorder.question, inquirer.Text)
    assert recorder.question.name == "app.name"
    assert recorder.question.message == "Enter the application name"
    assert recorder.question.default == "default-app"


def test_ask_hides_a_secret(interact):
    """A secret answer is asked with a Password, so what is typed is not echoed."""
    recorder = prompting("hunter2")
    with patch("opsmith.cli.interaction.inquirer.prompt", recorder):
        interact.ask("build_env.web.API_KEY", "Enter value for 'API_KEY'", secret=True)

    assert isinstance(recorder.question, inquirer.Password)


def test_the_validator_reports_its_own_message(interact):
    """
    A validator returning a problem is adapted into the ValidationError inquirer renders, with
    the message the core wrote rather than inquirer's generic one.
    """
    recorder = prompting("nope")
    with patch("opsmith.cli.interaction.inquirer.prompt", recorder):
        interact.ask("env.domain_email", "Email", validate=lambda value: "Needs an @.")

    with pytest.raises(inquirer_errors.ValidationError) as raised:
        recorder.question.validate("nope")

    assert raised.value.reason == "Needs an @."


def test_the_validator_passes_a_good_answer(interact):
    """A validator returning None accepts the value, so inquirer does not re-ask."""
    recorder = prompting("a@b.com")
    with patch("opsmith.cli.interaction.inquirer.prompt", recorder):
        interact.ask("env.domain_email", "Email", validate=lambda value: None)

    recorder.question.validate("a@b.com")


def test_an_unvalidated_answer_is_always_accepted(interact):
    """Without a validator every value passes, which is what inquirer's `validate=True` means."""
    recorder = prompting("anything")
    with patch("opsmith.cli.interaction.inquirer.prompt", recorder):
        interact.ask("run.command", "Enter the command")

    recorder.question.validate("anything")


def test_select_returns_the_value_not_the_label(interact):
    """The choice's value is what reaches the caller; the label only ever reaches the screen."""
    choices = [
        Choice(label="AWS - Amazon Web Services", value="AWS"),
        Choice(label="GCP - Google Cloud", value="GCP"),
    ]
    recorder = prompting("GCP")
    with patch("opsmith.cli.interaction.inquirer.prompt", recorder):
        answer = interact.select("env.cloud_provider", "Select a provider", choices)

    assert answer == "GCP"
    assert recorder.question.choices == [
        ("AWS - Amazon Web Services", "AWS"),
        ("GCP - Google Cloud", "GCP"),
    ]


def test_select_marks_and_defaults_to_the_recommendation(interact):
    """
    A recommended choice is labelled as such and offered first, which is how the model's pick
    was surfaced before Choice existed.
    """
    choices = [
        Choice(label="t4g.small (2 vCPUs)", value="t4g.small"),
        Choice(label="t4g.medium (4 vCPUs)", value="t4g.medium", recommended=True),
    ]
    recorder = prompting("t4g.medium")
    with patch("opsmith.cli.interaction.inquirer.prompt", recorder):
        interact.select("env.instance_type", "Select an instance type", choices)

    assert recorder.question.choices == [
        ("t4g.small (2 vCPUs)", "t4g.small"),
        ("t4g.medium (4 vCPUs) (Recommended)", "t4g.medium"),
    ]
    assert recorder.question.default == "t4g.medium"


def test_an_explicit_default_beats_the_recommendation(interact):
    """A caller that names a default gets it, even when a choice is marked recommended."""
    choices = [
        Choice(label="one", value="one"),
        Choice(label="two", value="two", recommended=True),
    ]
    recorder = prompting("one")
    with patch("opsmith.cli.interaction.inquirer.prompt", recorder):
        interact.select("env.action", "What now?", choices, default="one")

    assert recorder.question.default == "one"


def test_confirm_builds_a_confirm_with_its_default(interact):
    """confirm() keeps the default the caller chose, which decides what enter means."""
    recorder = prompting(False)
    with patch("opsmith.cli.interaction.inquirer.prompt", recorder):
        answer = interact.confirm("update.confirm_infra_changes", "Continue?", default=False)

    assert answer is False
    assert isinstance(recorder.question, inquirer.Confirm)
    assert recorder.question.default is False


def test_edit_seeds_the_editor_with_the_content(interact):
    """
    The document being reviewed is passed as content, not read from disk: the compose and
    Dockerfile proposals are the last failing attempt and have no file yet.
    """
    recorder = prompting("FROM python:3.12\n")
    with patch("opsmith.cli.interaction.inquirer.prompt", recorder):
        edited = interact.edit(
            "dockerfile.edit",
            "Edit the Dockerfile?",
            content="FROM python:3.11\n",
            path=Path("/repo/.opsmith/docker/api/Dockerfile"),
            on_headless="fail",
        )

    assert edited == "FROM python:3.12\n"
    assert isinstance(recorder.question, inquirer.Editor)
    assert recorder.question.default == "FROM python:3.11\n"


def test_a_cancelled_prompt_raises_interaction_cancelled(interact):
    """
    Ctrl-C reaches the caller as an InteractionCancelled naming the key, rather than as the
    None that every call site used to have to check for.
    """

    def cancel(questions, raise_keyboard_interrupt=False):
        raise KeyboardInterrupt()

    with patch("opsmith.cli.interaction.inquirer.prompt", cancel):
        with pytest.raises(InteractionCancelled) as raised:
            interact.select("env.region", "Select an AWS region", [Choice(label="a", value="a")])

    assert raised.value.details["key"] == "env.region"
    assert EXIT_CODES[raised.value.code] == 3


def test_a_prompt_that_answers_nothing_is_a_cancellation(interact):
    """inquirer returns None when it gives up on a question; that is a cancel, not an answer."""
    with patch("opsmith.cli.interaction.inquirer.prompt", return_value=None):
        with pytest.raises(InteractionCancelled):
            interact.ask("app.name", "Enter the application name")


def test_a_prompt_clears_the_spinner_first(interact, renderer):
    """A live spinner and a prompt would draw over each other, so the prompt clears it."""
    recorder = prompting("acme")
    with patch("opsmith.cli.interaction.inquirer.prompt", recorder):
        interact.ask("app.name", "Enter the application name")

    assert renderer.stop_waiting_calls == 1


def test_notify_reports_through_the_renderer(interact, renderer):
    """
    notify() emits an event rather than printing, so it is styled in text mode and streamed in
    JSON mode like everything else the run reports.
    """
    interact.notify("Invalid service configuration: name is required.", details={"path": "name"})

    event = renderer.events[-1]
    assert event.kind == "log"
    assert event.message == "Invalid service configuration: name is required."
    assert event.data == {"path": "name"}


def test_wait_for_returns_as_soon_as_the_check_passes(interact):
    """Nothing is asked when what the run is waiting for has already happened."""
    with patch("opsmith.cli.interaction.inquirer.prompt", prompting(True)) as prompt:
        interact.wait_for("dns.confirm", "Create the records", check=lambda: True)

    assert prompt.questions == []


def test_wait_for_rechecks_until_the_check_passes(interact):
    """A user who says to check again gets another check, and the wait ends when it passes."""
    checks = [False, True]
    with patch("opsmith.cli.interaction.inquirer.prompt", prompting(True)):
        interact.wait_for("dns.confirm", "Create the records", check=lambda: checks.pop(0))

    assert checks == []


def test_wait_for_cancels_when_the_user_stops_waiting(interact):
    """Declining to check again gives up on the run rather than continuing without the records."""
    with patch("opsmith.cli.interaction.inquirer.prompt", prompting(False)):
        with pytest.raises(InteractionCancelled):
            interact.wait_for("dns.confirm", "Create the records", check=lambda: False)
