"""What a run carries with it.

Everything a core module needs that it must not build for itself lives here: where the source is,
where to report progress, which model to ask, and where provisioners come from. A strategy or a
detector is handed one of these and reaches for nothing else, which is what makes both of them
constructible in a test.
"""

from pathlib import Path
from typing import Any, Optional

from pydantic_ai import Agent

from opsmith.core.events import EventSink
from opsmith.core.provisioners import ProvisionerFactory
from opsmith.git_repo import GitRepo


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
        agent: Optional[Agent] = None,
        provisioner_factory: Optional[ProvisionerFactory] = None,
        git_repo: Optional[GitRepo] = None,
        verbose: bool = False,
    ):
        """
        :param src_dir: The repository the run operates on.
        :param deployments_path: The ``.opsmith`` directory inside it.
        :param events: Where progress is reported.
        :param agent: The configured model. Absent only in tests that never call one.
        :param provisioner_factory: Where terraform and ansible provisioners come from.
        :param git_repo: An already-built repository handle. Left out in normal use, where it is
            opened lazily; supplied by tests that have no repository on disk.
        :param verbose: Whether the run was asked for detailed output.
        """
        self.src_dir = src_dir
        self.deployments_path = deployments_path
        self.events = events
        self.agent = agent
        self.provisioner_factory = provisioner_factory
        self.verbose = verbose
        self._git_repo = git_repo

        # Recorded by the CLI when a command that requires terraform checks for it, so a later
        # step can read the version without probing again.
        self.terraform_version: Optional[str] = None

        # Filled in by later parts of phase 0. Declared here so those parts only have to assign
        # them, and so a strategy can check for one without knowing which part shipped it.
        self.interact: Optional[Any] = None  # 0d: the Interaction implementation
        self.answers: Optional[Any] = None  # 0e: the persisted answer store
        self.steps: Optional[Any] = None  # 0e: the ledger for non-idempotent steps

    @property
    def git_repo(self) -> GitRepo:
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
