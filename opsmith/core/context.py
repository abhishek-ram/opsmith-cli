"""What a run carries with it.

Everything a core module needs that it must not build for itself lives here: where the source is,
where to report progress, which model to ask, and where provisioners come from. A strategy or a
detector is handed one of these and reaches for nothing else, which is what makes both of them
constructible in a test.
"""

from pathlib import Path
from typing import Optional

from pydantic_ai import Agent

from opsmith.agent import AgentDeps
from opsmith.core.answers import AnswerStore
from opsmith.core.events import EventSink
from opsmith.core.interaction import Interaction
from opsmith.core.provisioners import Provisioners
from opsmith.core.steps import StepLedger
from opsmith.git_repo import GitRepo, Repository
from opsmith.utils import project_state_dir


class OpsmithContext:
    """The handle every core module works through.

    Built once, in ``opsmith/cli/app.py``, and passed down. Nothing on it knows about a terminal:
    the CLI keeps the renderer and the output mode to itself and puts only the sink here.
    """

    def __init__(
        self,
        src_dir: Path,
        deployments_path: Path,
        events: EventSink,
        interact: Interaction,
        provisioner_factory: Provisioners,
        agent: Optional[Agent[AgentDeps, str]] = None,
        git_repo: Optional[Repository] = None,
        verbose: bool = False,
        answers: Optional[AnswerStore] = None,
    ):
        """
        The first five are how a run reaches the world, so every run has them: a context without
        one is not a state the CLI can be in, and defaulting one to None only turns a missing
        dependency into an AttributeError at the moment it is finally used.

        :param src_dir: The repository the run operates on.
        :param deployments_path: The ``.opsmith`` directory inside it.
        :param events: Where progress is reported.
        :param interact: How the run asks a person something.
        :param provisioner_factory: Where terraform and ansible provisioners come from.
        :param agent: The configured model. Optional because a real run has none until the model
            is resolved, which happens after the context is built so that ``--help`` does not
            demand the configuration it is explaining.
        :param git_repo: An already-built repository handle. Left out in normal use, where it is
            opened lazily; supplied by tests that have no repository on disk.
        :param verbose: Whether the run was asked for detailed output.
        :param answers: What this environment has already been asked. Built here when it is not
            supplied; the CLI supplies one because it has to hand the same store to the
            interaction it builds against it, and that happens before there is a context.
        """
        state_dir = project_state_dir(deployments_path)
        self.src_dir = src_dir
        self.deployments_path = deployments_path
        self.events = events
        self.interact = interact
        self.provisioner_factory = provisioner_factory
        self.agent = agent
        self.verbose = verbose
        self._git_repo = git_repo

        # Where an answer already given is read from, and where a new one is written. Never
        # None: a strategy asks a question wherever it needs one, and should not have to check
        # whether anybody remembered to build the thing that remembers the answer. Neither of
        # these is in the repository - see `project_state_dir` for why.
        self.answers = answers if answers is not None else AnswerStore(state_dir)

        # Not a parameter: a ledger is decided entirely by the path, so there is nothing for a
        # caller to choose and nothing to share it with.
        self.steps = StepLedger(state_dir)

        # Recorded by the CLI when a command that requires terraform checks for it, so a later
        # step can read the version without probing again.
        self.terraform_version: Optional[str] = None

    def require_agent(self) -> Agent[AgentDeps, str]:
        """
        The configured model, for a step that cannot do its work without one.

        ``agent`` is optional on the context because it is resolved after the context is built,
        but every command body runs with one: ``handle_errors`` resolves the model just before
        it. A step reaching this with nothing set is an invariant violation rather than anything
        a user did, so it raises rather than returning None for each call site to re-check.

        :return: The configured agent.
        :raises RuntimeError: The context was built without a model and never given one.
        """
        if self.agent is None:
            raise RuntimeError(
                "This step needs the model, but the context has no agent. The agent is resolved"
                " before a command body runs; a context without one is a wiring mistake."
            )
        return self.agent

    @property
    def git_repo(self) -> Repository:
        """
        The repository the run operates on, opened on first use.

        Opening is deferred because not every command needs a repository, and a command that does
        not should not fail outside one.

        :return: The repository handle.
        :raises NotAGitRepository: The source directory is not inside a git repository.
        """
        if self._git_repo is None:
            self._git_repo = GitRepo(self.src_dir, events=self.events)
        return self._git_repo
