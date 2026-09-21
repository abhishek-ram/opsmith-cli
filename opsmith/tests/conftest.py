"""Shared fixtures for the Opsmith test suite.

The fakes here stand in for the four things that make a run touch the world: the event sink, the
person at the keyboard, the git repository, and the terraform and ansible provisioners. With all
of them replaced a deployment strategy runs end to end in a test and every variable it would have
applied can be asserted.
"""

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from unittest.mock import MagicMock

import git
import pytest
import rich
from typer.testing import CliRunner

from opsmith.core.context import OpsmithContext
from opsmith.core.errors import InteractionCancelled, OpsmithError
from opsmith.core.events import Event, EventSink
from opsmith.core.interaction import Notice, next_steps_of
from opsmith.settings import settings


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """
    Builds an empty Opsmith project: a git repository containing an empty `.opsmith/`.

    :param tmp_path: pytest's per-test temporary directory.
    :return: The source directory of the project.
    """
    src_dir = tmp_path / "project"
    src_dir.mkdir()
    git.Repo.init(str(src_dir))
    (src_dir / ".opsmith").mkdir()
    return src_dir


@pytest.fixture
def runner() -> CliRunner:
    """
    Returns a Typer CLI runner that keeps stdout and stderr apart.

    Separate streams are what lets a test assert that stdout carries exactly one JSON
    envelope and nothing else.

    :return: A configured CliRunner.
    """
    return CliRunner()


@pytest.fixture(autouse=True)
def reset_rich_console():
    """
    Restores rich's global console after every test.

    Running a command in JSON mode reconfigures that console to write to stderr, and it is
    process-wide state that would otherwise leak into the tests that follow.
    """
    original = rich._console
    yield
    rich._console = original


@pytest.fixture(autouse=True)
def no_real_state_dir(tmp_path: Path, monkeypatch):
    """
    Keeps every test's remembered answers inside its own temporary directory.

    What Opsmith remembers about a project lives outside the project, under the home directory.
    It is autouse because a test that reached the real one would write a developer's own machine,
    read back another test's answers, and pass or fail depending on what had run before it.
    """
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "opsmith-state"))


class FakeDns:
    """The DNS a run can see, so no test ever asks a real resolver.

    Everything Opsmith asks for is published by default, which is the state a deploy proceeds
    through; a test that is about waiting says so, and then only the records it names resolve.
    """

    def __init__(self):
        #: The records that resolve, or None while every record does.
        self.published: Optional[List[Dict[str, str]]] = None
        #: Every record that was looked up, in order.
        self.lookups: List[Dict[str, str]] = []

    def publish_nothing(self):
        """Makes every record look as though it has not been created yet."""
        self.published = []

    def publish(self, *records: Dict[str, str]):
        """
        Makes exactly these records resolve, and no others.

        :param records: The records that have been created.
        """
        self.published = list(records)

    def is_published(self, record: Dict[str, str]) -> bool:
        """
        Stands in for the resolver.

        :param record: The record being checked for.
        :return: Whether it resolves.
        """
        self.lookups.append(record)
        return True if self.published is None else record in self.published


@pytest.fixture
def dns() -> FakeDns:
    """The DNS a run can see. Ask it to publish nothing to exercise a wait."""
    return FakeDns()


@pytest.fixture(autouse=True)
def no_real_dns(dns: FakeDns, monkeypatch):
    """
    Replaces the resolver everywhere, for every test.

    It is autouse because a DNS lookup that escaped would be slow, would depend on the network,
    and would answer differently on someone else's machine.
    """
    monkeypatch.setattr("opsmith.utils.dns_record_is_published", dns.is_published)
    monkeypatch.setattr(
        "opsmith.deployment_strategies.monolithic.dns_record_is_published", dns.is_published
    )


class RecordingSink(EventSink):
    """Keeps every event, so a test can assert on what a run reported."""

    def __init__(self):
        self.events: List[Event] = []

    def emit(self, event: Event):
        """Records the event."""
        self.events.append(event)

    def messages(self, kind: Optional[str] = None) -> List[str]:
        """
        Returns the messages recorded, optionally of one kind only.

        :param kind: Restrict to this event kind, or None for all of them.
        :return: The messages, in the order they were emitted.
        """
        return [e.message for e in self.events if kind is None or e.kind == kind]


class FakeInteraction:
    """Answers questions from a script, and records what was asked.

    Answers are scripted by interaction key. A question with no scripted answer is answered with
    the default the code offered, which is what a user pressing enter through the flow would do,
    so a test only has to name the answers it actually cares about.

    It refuses a scripted answer the real prompt would have refused - one that fails the
    question's own validator, or that is not among the offered choices - because a fake that is
    more permissive than the terminal hides the bug it was meant to catch.
    """

    def __init__(self, answers: Optional[Dict[str, Any]] = None):
        """
        :param answers: What to answer, by interaction key.
        """
        self.answers: Dict[str, Any] = dict(answers or {})
        self.asked: List[Dict[str, Any]] = []

        # The same two lists both real implementations keep, because a command copies them into
        # its result without knowing which implementation it is talking to.
        self.notices: List[Notice] = []
        self.next_steps: List[str] = []

    def _record(self, primitive: str, key: str, message: str, **fields) -> Dict[str, Any]:
        """
        Keeps one question, in the order it was asked.

        :param primitive: Which of the five was called.
        :param key: The interaction key.
        :param message: The question as it was worded.
        :param fields: Whatever else that primitive carried.
        :return: The recorded entry.
        """
        entry = {"primitive": primitive, "key": key, "message": message, **fields}
        self.asked.append(entry)
        return entry

    def _answer(self, key: str, fallback: Any) -> Any:
        """
        :param key: The interaction key being answered.
        :param fallback: What to answer when the test did not script this key.
        :return: The answer.
        """
        return self.answers[key] if key in self.answers else fallback

    def ask(self, key, message, *, default=None, secret=False, validate=None):
        """Answers a typed value, refusing one its own validator would reject."""
        self._record("ask", key, message, default=default, secret=secret)
        answer = self._answer(key, default)
        if validate is not None and answer is not None:
            problem = validate(answer)
            assert problem is None, f"the answer scripted for '{key}' is not valid: {problem}"
        return answer

    def select(self, key, message, choices, *, default=None):
        """Answers with one of the offered values, defaulting to the recommended choice."""
        self._record("select", key, message, default=default, choices=list(choices))

        fallback = default
        if fallback is None:
            recommended = [choice for choice in choices if choice.recommended]
            fallback = recommended[0].value if recommended else choices[0].value

        answer = self._answer(key, fallback)
        values = [choice.value for choice in choices]
        assert answer in values, f"the answer scripted for '{key}' is not one of the choices"
        return answer

    def confirm(self, key, message, *, details=None, default=False):
        """Answers yes or no, defaulting to what the code offered."""
        self._record("confirm", key, message, default=default, details=details)
        return self._answer(key, default)

    def edit(self, key, message, *, content, path=None, on_headless):
        """Returns the scripted document, or the proposal unchanged."""
        self._record("edit", key, message, path=path, on_headless=on_headless)
        return self._answer(key, content)

    def wait_for(self, key, message, *, check, details=None, timeout_s=None):
        """
        Returns once the check passes, or once the test says the wait was satisfied.

        :raises InteractionCancelled: The check fails and nothing said to carry on, which is
            what a person who gave up waiting would cause.
        """
        self._record("wait_for", key, message, details=details)
        if check():
            return
        if not self._answer(key, False):
            raise InteractionCancelled(key, message)

    def notify(self, message, *, details=None):
        """Keeps what the run told the user, the way the real implementations keep it."""
        self.notices.append(Notice(message=message, details=details))
        self.next_steps.extend(next_steps_of(details))


def hint_of(error: OpsmithError) -> str:
    """
    The hint an error carries, for a test asserting on its wording.

    ``hint`` is optional on the class because not every failure has advice to give. A test that
    reads one is asserting there is advice, so this says that once rather than at each call.

    :param error: The error a test caught.
    :return: Its hint.
    """
    assert error.hint is not None, f"{error.code} carried no hint"
    return error.hint


class FakeGitRepo:
    """A git repository that needs no repository on disk."""

    def __init__(self, archive_path: Optional[Path] = None):
        """
        :param archive_path: What ``git_archive_context`` yields as the clean build context.
        """
        self.archive_path = archive_path if archive_path is not None else Path("/fake/archive")
        self.ensure_gitignore_calls = 0

    def get_git_tracked_files(self, src_dirs: List[str]) -> List[Path]:
        """Returns no tracked files, which is enough for every strategy path."""
        return []

    @contextmanager
    def git_archive_context(self):
        """Yields the fixed archive path instead of exporting a real repository."""
        yield self.archive_path

    def ensure_gitignore(self):
        """Records that the caller asked for the opsmith ignore block."""
        self.ensure_gitignore_calls += 1


class FakeProvisioner:
    """Base for the recording provisioners. Every call lands in a shared, ordered log."""

    kind = "provisioner"

    def __init__(self, working_dir: Path, step: str, call_log: List[Dict[str, Any]]):
        self.working_dir = working_dir
        self.step = step
        self.call_log = call_log
        # The real provisioners create their working directory on construction, and destroy()
        # decides what to tear down by looking for those directories.
        self.working_dir.mkdir(parents=True, exist_ok=True)

    def _record(self, action: str, **fields) -> Dict[str, Any]:
        """
        Appends one call to the shared log.

        :param action: What was called, such as "apply" or "run_playbook".
        :param fields: The arguments worth asserting on.
        :return: The recorded entry.
        """
        entry = {
            "kind": self.kind,
            "action": action,
            "working_dir": self.working_dir,
            "step": self.step,
            **fields,
        }
        self.call_log.append(entry)
        return entry

    def copy_template(self, template_name: str, provider: str):
        """
        Records the template that would have been copied, and the provider exactly as it was
        passed. The real provisioner is what normalises the casing, and a test asserting on
        that has to see what the call site actually said.
        """
        self._record("copy_template", template=template_name, provider=provider)


class FakeTerraformProvisioner(FakeProvisioner):
    """Records terraform calls and returns scripted outputs."""

    kind = "terraform"

    def __init__(
        self,
        working_dir: Path,
        step: str,
        call_log: List[Dict[str, Any]],
        outputs: Dict[str, Any],
    ):
        super().__init__(working_dir, step, call_log)
        self.outputs = outputs

    def init_and_apply(
        self, variables: Mapping[str, Any], env_vars: Optional[Mapping[str, Any]] = None
    ):
        """Records the variables that would have been applied."""
        self._record("apply", variables=dict(variables), env_vars=dict(env_vars or {}))

    def destroy(self, variables: Mapping[str, Any], env_vars: Optional[Mapping[str, Any]] = None):
        """Records the variables that would have been destroyed with."""
        self._record("destroy", variables=dict(variables), env_vars=dict(env_vars or {}))

    def get_output(self) -> Dict[str, Any]:
        """Returns the outputs scripted for this working directory."""
        self._record("get_output")
        return dict(self.outputs)


class FakeAnsibleProvisioner(FakeProvisioner):
    """Records playbook runs and returns scripted outputs, or the scripted failure."""

    kind = "ansible"

    def __init__(
        self,
        working_dir: Path,
        step: str,
        call_log: List[Dict[str, Any]],
        outputs: Dict[str, str],
        failure: Optional[Exception] = None,
    ):
        super().__init__(working_dir, step, call_log)
        self.outputs = outputs
        self.failure = failure

    def run_playbook(
        self,
        playbook_name: str,
        extra_vars: Mapping[str, Any],
        inventory: Optional[str] = None,
        user: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        Records the extra vars the playbook would have received.

        :raises Exception: The failure scripted for this working directory, if there is one. It
            is raised from here rather than from the factory, because callers catch a failing
            *run* and a factory that raised would skip past their handling of it.
        """
        self._record(
            "run_playbook",
            playbook=playbook_name,
            extra_vars=dict(extra_vars),
            inventory=inventory,
        )
        if self.failure is not None:
            raise self.failure
        return dict(self.outputs)


class FakeProvisionerFactory:
    """Hands out recording provisioners and keeps one ordered log across all of them.

    Outputs are scripted per template name, because that is what a call site actually varies:
    ``outputs = {"container_registry": {"registry_url": "..."}}``.
    """

    def __init__(
        self,
        terraform_outputs: Optional[Dict[str, Dict[str, Any]]] = None,
        ansible_outputs: Optional[Dict[str, Dict[str, str]]] = None,
        ansible_failures: Optional[Dict[str, Exception]] = None,
    ):
        """
        :param terraform_outputs: Outputs keyed by the last path segment of the working directory.
        :param ansible_outputs: Playbook outputs keyed the same way.
        :param ansible_failures: Errors to raise from ``run_playbook``, keyed the same way.
        """
        self.terraform_outputs = terraform_outputs or {}
        self.ansible_outputs = ansible_outputs or {}
        self.ansible_failures: Dict[str, Exception] = ansible_failures or {}
        self.calls: List[Dict[str, Any]] = []

    def terraform(self, working_dir: Path, step: str = "provision") -> FakeTerraformProvisioner:
        """Builds a recording terraform provisioner."""
        return FakeTerraformProvisioner(
            working_dir=working_dir,
            step=step,
            call_log=self.calls,
            outputs=self._outputs_for(self.terraform_outputs, working_dir),
        )

    def ansible(self, working_dir: Path, step: str = "provision") -> FakeAnsibleProvisioner:
        """Builds a recording ansible provisioner."""
        return FakeAnsibleProvisioner(
            working_dir=working_dir,
            step=step,
            call_log=self.calls,
            outputs=self._outputs_for(self.ansible_outputs, working_dir),
            failure=self._outputs_for(self.ansible_failures, working_dir) or None,
        )

    @staticmethod
    def _outputs_for(scripted: Dict[str, Any], working_dir: Path) -> Any:
        """
        Finds what was scripted for a working directory: its outputs, or its failure.

        Matching walks up from the directory itself, so per-service directories such as
        ``frontend_cdn/<slug>`` are served by an entry keyed on ``frontend_cdn``.

        :param scripted: Outputs or failures, keyed by directory name.
        :param working_dir: The directory the provisioner runs in.
        :return: What was scripted, or an empty mapping when nothing was.
        """
        for part in reversed(working_dir.parts):
            if part in scripted:
                return scripted[part]
        return {}

    def actions(self, *kinds: str) -> List[str]:
        """
        Returns the ordered actions recorded, optionally of certain provisioner kinds only.

        :param kinds: Restrict to "terraform" and/or "ansible"; empty means both.
        :return: Labels such as "terraform:apply:container_registry".
        """
        return [
            f"{call['kind']}:{call['action']}:{call['working_dir'].name}"
            for call in self.calls
            if not kinds or call["kind"] in kinds
        ]

    def find(self, action: str, working_dir_name: str) -> Dict[str, Any]:
        """
        Returns the one recorded call matching an action and a working directory.

        :param action: The action to look for, such as "apply".
        :param working_dir_name: The last path segment of the working directory.
        :return: The recorded call.
        :raises AssertionError: No call, or more than one, matched.
        """
        matches = [
            call
            for call in self.calls
            if call["action"] == action and call["working_dir"].name == working_dir_name
        ]
        assert len(matches) == 1, (
            f"expected exactly one {action} in {working_dir_name}, got"
            f" {[c['working_dir'].name for c in self.calls if c['action'] == action]}"
        )
        return matches[0]


@pytest.fixture
def events() -> RecordingSink:
    """A sink that keeps everything a run reports."""
    return RecordingSink()


@pytest.fixture
def interact() -> FakeInteraction:
    """A stand-in for the person at the keyboard, answering with the code's own defaults."""
    return FakeInteraction()


@pytest.fixture
def provisioners() -> FakeProvisionerFactory:
    """A provisioner factory that records instead of shelling out."""
    return FakeProvisionerFactory()


@pytest.fixture
def git_repo(tmp_path: Path) -> FakeGitRepo:
    """A repository that needs no checkout on disk, and counts what was asked of it."""
    return FakeGitRepo(archive_path=tmp_path)


@pytest.fixture
def opsmith_context(
    tmp_path: Path,
    events: RecordingSink,
    provisioners,
    interact: FakeInteraction,
    git_repo: FakeGitRepo,
) -> OpsmithContext:
    """
    A context wired entirely to fakes, for testing core code with nothing on disk.

    :param tmp_path: pytest's per-test temporary directory, used as the source directory.
    :param events: The recording sink.
    :param provisioners: The recording provisioner factory.
    :return: A context that touches neither a repository nor a cloud.
    """
    return OpsmithContext(
        src_dir=tmp_path,
        deployments_path=tmp_path / ".opsmith",
        events=events,
        agent=MagicMock(),
        provisioner_factory=provisioners,
        interact=interact,
        git_repo=git_repo,
    )
