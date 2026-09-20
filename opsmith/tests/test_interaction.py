"""Tests for the two interactions: the one that prompts, and the one that cannot.

The first half is the terminal implementation - how each primitive becomes an inquirer question.
The second is the headless one, which answers from what the run was told and stops the run when
nothing has the answer.
"""

from pathlib import Path
from typing import List
from unittest.mock import patch

import inquirer
import pytest
import yaml
from inquirer import errors as inquirer_errors

from opsmith.cli.interaction import TerminalInteraction
from opsmith.core.answers import AnswerSources, AnswerStore
from opsmith.core.errors import (
    EXIT_CODES,
    InteractionCancelled,
    InvalidArgument,
    InvalidConfig,
    ManualEditRequired,
    MissingAnswerError,
    PendingActionError,
)
from opsmith.core.interaction import Choice, HeadlessInteraction
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


# --- the headless interaction ----------------------------------------------------------------


class FakeClock:
    """A clock that only moves when something waits on it.

    Injecting this is what lets a test prove a ten minute timeout in no time at all, and it also
    means a wait that forgot to sleep would spin forever rather than quietly passing.
    """

    def __init__(self):
        self.now = 0.0
        self.slept: List[float] = []

    def monotonic(self) -> float:
        """:return: The current reading."""
        return self.now

    def sleep(self, seconds: float):
        """
        :param seconds: How long to wait, which is how far the clock moves.
        """
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    """The clock a headless wait reads and moves."""
    return FakeClock()


@pytest.fixture
def store(tmp_path: Path) -> AnswerStore:
    """A store bound to an environment, so an answer given is an answer remembered."""
    store = AnswerStore(tmp_path / ".opsmith")
    store.use_environment("prod")
    return store


@pytest.fixture
def headless(renderer: FakeRenderer, store: AnswerStore, clock: FakeClock):
    """
    Builds a headless interaction told whatever the test says it was told.

    :return: A factory taking the same options the command line carries.
    """

    def build(wait_timeout: int = 600, **options) -> HeadlessInteraction:
        return HeadlessInteraction(
            AnswerSources(**options),
            store,
            events=renderer,
            wait_timeout=wait_timeout,
            resume="opsmith deploy",
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    return build


REGIONS = [
    Choice(label="US East (N. Virginia)", value="us-east-1"),
    Choice(label="EU West (Ireland)", value="eu-west-1", recommended=True),
]


def test_a_headless_run_answers_from_what_it_was_told(headless):
    """An inline answer is the most direct thing a caller can say, and it is taken as given."""
    interact = headless(inline={"env.region": "us-east-1"})

    assert interact.ask("env.region", "Which region") == "us-east-1"


def test_a_headless_run_remembers_what_it_answered(headless, store: AnswerStore):
    """
    The answer is written the moment it is given. A run that stops at the next question has to
    find this one already answered when it is run again.
    """
    interact = headless(inline={"env.region": "us-east-1"})
    interact.ask("env.region", "Which region")

    assert store.get("env.region") == "us-east-1"


def test_a_question_already_answered_is_not_asked_again(headless, store: AnswerStore):
    """This is the resume: the second invocation is told nothing, and needs to be told nothing."""
    store.record("env.region", "us-east-1")

    assert headless().ask("env.region", "Which region") == "us-east-1"


def test_a_secret_answer_is_remembered_apart(headless, store: AnswerStore):
    """
    A value asked for as a secret is routed to the secret half of the store, so it never reaches
    the file that is meant to be committed.
    """
    interact = headless(env_file={"envvar.DATABASE_URL": "postgres://host/db"})
    interact.ask("envvar.DATABASE_URL", "Enter value for DATABASE_URL", secret=True)

    assert store.get("envvar.DATABASE_URL") == "postgres://host/db"
    assert yaml.safe_load(store.answers_path.read_text(encoding="utf-8")) in (None, {})


def test_a_question_nobody_answered_stops_the_run(headless):
    """
    The stop carries everything a driver needs to answer it and try again: the key, the shape of
    the answer, the variable that would carry it, and the command to run.
    """
    with pytest.raises(MissingAnswerError) as raised:
        headless().ask("env.domain_email", "Enter email for SSL")

    details = raised.value.details
    assert details["key"] == "env.domain_email"
    assert details["primitive"] == "ask"
    assert details["env_var"] == "OPSMITH_ANSWER_ENV_DOMAIN_EMAIL"
    assert details["resume"] == "opsmith deploy"
    assert EXIT_CODES[raised.value.code] == 3


def test_a_missing_secret_is_not_asked_for_on_a_command_line(headless):
    """
    The hint for a secret names the environment variable instead of the flag, because a command
    line is readable by every process on the machine and lands in a shell history.
    """
    with pytest.raises(MissingAnswerError) as raised:
        headless().ask("envvar.SECRET_KEY", "Enter value for SECRET_KEY", secret=True)

    assert "--answer" not in raised.value.hint
    assert "OPSMITH_ANSWER_ENVVAR_SECRET_KEY" in raised.value.hint
    assert raised.value.details["secret"] is True


def test_an_option_can_be_chosen_by_its_value_or_by_its_label(headless):
    """
    A flag can only ever carry text, and the label is what a person reads in the terminal, so
    both are ways of naming the same option.
    """
    assert (
        headless(inline={"env.region": "eu-west-1"}).select("env.region", "?", REGIONS)
        == "eu-west-1"
    )
    assert (
        headless(inline={"env.region": "US East (N. Virginia)"}).select("env.region", "?", REGIONS)
        == "us-east-1"
    )


def test_an_option_whose_value_is_an_object_can_still_be_chosen_and_remembered(
    headless, store: AnswerStore
):
    """
    An instance type is a whole model, which no flag could carry and no YAML file should hold. It
    is named by its label, and the label is what the store keeps.
    """
    machines = [
        Choice(label="t3.small", value={"name": "t3.small", "cpu": 2}),
        Choice(label="t3.large", value={"name": "t3.large", "cpu": 4}),
    ]
    interact = headless(inline={"env.instance_type": "t3.large"})

    chosen = interact.select("env.instance_type", "Which machine", machines)

    assert chosen == {"name": "t3.large", "cpu": 4}
    assert store.get("env.instance_type") == "t3.large"


def test_a_stop_on_a_choice_says_what_the_choices_were(headless):
    """Otherwise a driver has to guess what it is allowed to say."""
    with pytest.raises(MissingAnswerError) as raised:
        headless().select("env.region", "Which region", REGIONS)

    assert raised.value.details["choices"] == [
        {"label": "US East (N. Virginia)", "value": "us-east-1"},
        {"label": "EU West (Ireland)", "value": "eu-west-1"},
    ]


def test_an_answer_the_question_refuses_is_a_usage_error(headless):
    """
    Reporting a supplied answer as missing would tell the driver to supply what it just supplied,
    and it would do exactly that, forever.
    """
    with pytest.raises(InvalidArgument) as raised:
        headless(inline={"env.region": "mars-north-1"}).select("env.region", "?", REGIONS)

    assert EXIT_CODES[raised.value.code] == 2
    assert raised.value.details["source"] == "--answer"


def test_an_answer_that_fails_its_own_rule_is_a_usage_error(headless):
    """The rule a person reads at the prompt is the rule a supplied answer is held to."""

    def must_be_an_email(value):
        return None if "@" in value else "Enter an email address."

    with pytest.raises(InvalidArgument) as raised:
        headless(inline={"env.domain_email": "nope"}).ask(
            "env.domain_email", "Enter email", validate=must_be_an_email
        )

    assert raised.value.details["problem"] == "Enter an email address."


def test_a_remembered_answer_that_no_longer_fits_is_dropped_rather_than_fatal(
    headless, store: AnswerStore, renderer: FakeRenderer
):
    """
    The store is Opsmith's own. A stale entry in it is not something the person running the
    command did, so it must not wedge the run with an error they cannot act on.
    """
    store.record("env.region", "ap-south-1")

    with pytest.raises(MissingAnswerError):
        headless().select("env.region", "Which region", REGIONS)

    assert any(event.kind == "warning" for event in renderer.events)


def test_accept_defaults_takes_the_recommended_option(headless):
    """The recommendation is the code's own answer, which is what a default is."""
    interact = headless(accept_defaults=True)

    assert interact.select("env.region", "Which region", REGIONS) == "eu-west-1"


def test_accept_defaults_does_not_invent_an_answer_nobody_offered(headless):
    """
    With no default and no recommendation there is nothing to accept, and picking the first
    option would be Opsmith choosing a region for somebody.
    """
    plain = [Choice(label="a", value="a"), Choice(label="b", value="b")]

    with pytest.raises(MissingAnswerError):
        headless(accept_defaults=True).select("env.region", "Which region", plain)


def test_a_destructive_gate_is_answered_by_naming_it(headless):
    """
    There is no blanket flag for these. Approving what a run is about to change means saying so
    for that gate, by key, which is the same mechanism every other answer uses.
    """
    interact = headless(inline={"update.confirm_infra_changes": "true"})

    assert interact.confirm("update.confirm_infra_changes", "Continue?") is True


def test_the_deletion_gate_still_wants_its_word(headless):
    """
    On a terminal the gate asks for a word to be typed, and that friction stays. Without a person
    the word is supplied the same way, by key, rather than by a flag that means "whatever it asks".
    """
    interact = headless(inline={"delete.confirm": "DELETE"})

    assert interact.ask("delete.confirm", "Type DELETE") == "DELETE"


def test_nothing_blanket_answers_a_destructive_gate(headless):
    """
    The point of removing the blanket flag: a run told to take every shortcut it can still stops
    at the gate, because destroying an environment is not a shortcut anybody can take in advance.
    """
    interact = headless(accept_defaults=True)

    with pytest.raises(MissingAnswerError) as raised:
        interact.ask("delete.confirm", "Type DELETE")

    assert raised.value.details["key"] == "delete.confirm"


def test_accept_defaults_does_not_agree_to_something_destructive(headless):
    """
    Taking a default is a statement that the ordinary answer will do. Destroying an environment
    has no ordinary answer.
    """
    with pytest.raises(MissingAnswerError):
        headless(accept_defaults=True).confirm(
            "update.confirm_infra_changes", "Continue?", default=True
        )


def test_a_destructive_confirmation_is_never_remembered(headless, store: AnswerStore):
    """Otherwise every later run would destroy without being asked."""
    headless(inline={"update.confirm_infra_changes": "true"}).confirm(
        "update.confirm_infra_changes", "Continue?"
    )

    assert store.get("update.confirm_infra_changes") is None


def test_a_review_editor_accepts_what_was_proposed(headless):
    """There is nobody to review it, and the proposal is what the model actually suggested."""
    assert (
        headless().edit("infra_deps.confirm", "Review", content="a: 1", on_headless="accept")
        == "a: 1"
    )


def test_a_review_editor_asked_twice_stops_instead_of_looping(headless):
    """
    Being asked again means what was accepted did not parse. Accepting it a second time would not
    parse either, and the caller's loop would never end - which is worse than stopping.
    """
    interact = headless()
    interact.edit("infra_deps.confirm", "Review", content="not valid", on_headless="accept")

    with pytest.raises(InvalidConfig):
        interact.edit("infra_deps.confirm", "Review", content="not valid", on_headless="accept")


def test_a_fix_editor_leaves_the_document_where_it_says_it_did(headless, tmp_path: Path):
    """
    The stop tells somebody to edit a file, so the file has to be there and has to hold the last
    thing the model produced - otherwise there is nothing to edit.
    """
    path = tmp_path / "docker" / "api" / "Dockerfile"

    with pytest.raises(ManualEditRequired) as raised:
        headless().edit(
            "dockerfile.edit",
            "Fix the Dockerfile",
            content="FROM python:3.13\n",
            path=path,
            on_headless="fail",
        )

    assert path.read_text(encoding="utf-8") == "FROM python:3.13\n"
    assert raised.value.details["path"] == str(path)
    assert EXIT_CODES[raised.value.code] == 4


def test_a_wait_that_is_already_satisfied_does_not_wait(headless, clock: FakeClock):
    """The records were created before the command ran, which is the happy case for a re-run."""
    headless().wait_for("dns.api", "Create the record", check=lambda: True)

    assert clock.slept == []


def test_a_wait_polls_until_the_thing_has_been_done(headless, clock: FakeClock):
    """It is somebody else's action, and it takes as long as it takes."""
    attempts = []

    def check():
        attempts.append(clock.now)
        return len(attempts) >= 3

    headless().wait_for("dns.api", "Create the record", check=check)

    assert len(attempts) == 3
    assert clock.slept  # it waited between them rather than spinning


def test_a_wait_that_times_out_says_what_is_still_missing(headless, clock: FakeClock):
    """
    Exit 8 is a different instruction from exit 3: nobody has to supply an answer, somebody has
    to go and do something, and then run the same command again.
    """
    records = [{"type": "A", "name": "api.example.test", "value": "203.0.113.10"}]

    with pytest.raises(PendingActionError) as raised:
        headless(wait_timeout=60).wait_for(
            "dns.api",
            "Create the record",
            check=lambda: False,
            details={"records": records},
        )

    assert EXIT_CODES[raised.value.code] == 8
    assert raised.value.details["records"] == records
    assert raised.value.details["resume"] == "opsmith deploy"
    assert raised.value.details["waited_s"] >= 60


def test_a_wait_already_satisfied_once_is_not_polled_again(headless, store: AnswerStore):
    """
    A run that stopped later and was run again should not go back to the network to re-establish
    something it already established.
    """
    store.record("dns.api", True)
    checked = []

    headless().wait_for("dns.api", "Create the record", check=lambda: checked.append(1))

    assert checked == []


def test_a_wait_the_caller_bounds_is_never_given_longer_than_the_run_allows(
    headless, clock: FakeClock
):
    """`--wait-timeout` is the ceiling; a caller with its own opinion may only be stricter."""
    with pytest.raises(PendingActionError) as raised:
        headless(wait_timeout=30).wait_for(
            "dns.api", "Create the record", check=lambda: False, timeout_s=600
        )

    assert raised.value.details["timeout_s"] == 30


def test_what_the_run_tells_the_user_is_kept_for_the_report(headless):
    """
    With nobody watching the terminal, a notice still has to reach whoever reads the run - so it
    is both an event and something the result can carry.
    """
    interact = headless()
    interact.notify("Your site is live", details={"next_steps": ["point your domain at it"]})

    assert [notice.model_dump() for notice in interact.notices] == [
        {"message": "Your site is live", "details": {"next_steps": ["point your domain at it"]}}
    ]
    assert interact.next_steps == ["point your domain at it"]


@pytest.mark.parametrize("field", ["message", "step", "kind", "data"])
def test_a_notice_whose_details_are_named_after_the_event_still_reports(headless, renderer, field):
    """
    Details are spread across the event's data, so one named after a field of the event itself
    used to pass that field twice and raise. A ConfigIssue has a message, and reporting a problem
    with an edited service is exactly the path that hits it, so the nesting is not theoretical.
    """
    interact = headless()

    interact.notify("Invalid service configuration", details={field: "services.0.service_port"})

    reported = renderer.events[-1]
    assert reported.message == "Invalid service configuration"
    assert reported.data == {"details": {field: "services.0.service_port"}}


def test_details_that_cannot_collide_are_still_reported_by_name(headless, renderer):
    """
    The nesting above is only for the names that would collide. Everything else stays spread, so
    a reader of the event stream sees the fields the caller named.
    """
    interact = headless()

    interact.notify("Waiting on DNS", details={"records": [{"type": "A"}]})

    assert renderer.events[-1].data == {"records": [{"type": "A"}]}
