import io
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, List, Optional

from opsmith.core.errors import GitNotAvailable, NotAGitRepository
from opsmith.core.events import STEP_BUILD, STEP_SETUP, EventSink, resolve_sink


def _import_git() -> Any:
    """
    Imports GitPython, which looks for the git executable while it is being imported.

    That search is why this is not an import at the top of the module: it raises when it finds
    nothing, and a module-level import would therefore make importing any part of Opsmith fail on
    a machine without git - long before anything asked for a repository. ``opsmith env list`` and
    ``opsmith env status`` read files this package wrote and need no tooling at all, so the cost
    of git is paid here, where a repository is actually being opened.

    :return: The ``git`` module.
    :raises GitNotAvailable: git is not installed, or is not on the PATH.
    """
    try:
        import git
    except ImportError as err:
        raise GitNotAvailable(
            "git is not installed, or is not on the PATH.",
            hint="Install git and make sure it is on your PATH, then run the command again.",
            details={"problem": str(err).splitlines()[0]},
        ) from err

    return git


class GitRepo:
    def __init__(self, root_dir: Path, events: Optional[EventSink] = None):
        """
        Initialize a Git repository object for the specified directory, or its parent directories
        if it is a subdirectory, ensuring it belongs to an actual Git repository.

        :param root_dir: The root directory or subdirectory intended for Git repository initialization.
        :type root_dir: Path
        :param events: Sink to report progress to. Defaults to discarding it.

        :raises NotAGitRepository: If the specified directory or its parent directories do not
            contain a valid Git repository.
        """
        self.events = resolve_sink(events)
        self._git = _import_git()
        try:
            # Initialize repo object, searching upwards from root_dir if it's a subdirectory
            self.repo = self._git.Repo(str(root_dir), search_parent_directories=True)

        except self._git.exc.InvalidGitRepositoryError:
            raise NotAGitRepository(
                f"'{root_dir}' is not a git repository, or git is not found in PATH.",
                hint=(
                    "Run 'git init' in the source directory, or point --src-dir at a repository."
                    " Check that git is installed and on your PATH."
                ),
                details={"src_dir": str(root_dir)},
            )

    @contextmanager
    def _reporting_a_missing_git(self) -> Iterator[None]:
        """
        Turns a missing git executable into an error the CLI can map, wherever Opsmith shells out.

        Opening a repository only reads the files under ``.git``, so it succeeds on a machine with
        no git at all; the binary is not needed until something asks git to do something. Without
        this, that would surface as an unhandled exception and be reported as a bug in Opsmith.

        :raises GitNotAvailable: git could not be run.
        """
        try:
            yield
        except self._git.exc.GitCommandNotFound as err:
            raise GitNotAvailable(
                "git is installed but could not be run.",
                hint="Check that git works, and that it is on the PATH this command was given.",
                details={"problem": str(err).splitlines()[0]},
            ) from err

    def get_git_tracked_files(self, src_dirs: List[str]) -> List[Path]:
        """
        Retrieves a list of absolute file paths that are tracked by Git within the
        specified source directories.

        The function searches for files tracked by Git within the provided directories,
        respecting the .gitignore file. If there are no tracked files, an empty list
        is returned. The file paths returned are converted into absolute paths relative
        to the Git repository's root directory.

        :param src_dirs: List of source directories (as strings) to scan for Git-tracked
                         files.
        :type src_dirs: List[str]

        :return: A list of absolute file paths for files tracked by Git within the
                 specified source directories.
        :rtype: List[Path]
        """
        # Searching upwards from root_dir if it's a subdirectory
        git_root = Path(self.repo.working_dir)

        # List tracked, cached, and other files (respecting .gitignore)
        # The paths are relative to the git_root.
        ls_files_args = ["-c", "--exclude-standard"]
        ls_files_args.extend(src_dirs)

        with self._reporting_a_missing_git():
            tracked_files_str = self.repo.git.ls_files(*ls_files_args)

        if not tracked_files_str:  # Handle case where there are no tracked files
            return []

        relative_paths = tracked_files_str.strip().split("\n")

        # Construct absolute paths and filter out potential empty strings from split
        absolute_paths = [git_root / p for p in relative_paths if p]
        return absolute_paths

    @contextmanager
    def git_archive_context(self):
        """Creates a clean build context from git-tracked files only."""
        self.events.log(STEP_BUILD, "Creating build context from git-tracked files...")
        with tempfile.TemporaryDirectory() as temp_dir:
            buf = io.BytesIO()
            with self._reporting_a_missing_git():
                self.repo.archive(buf, format="tar")
            buf.seek(0)
            with tarfile.open(fileobj=buf) as tar:
                tar.extractall(path=temp_dir)
            yield Path(temp_dir)

    def ensure_gitignore(self):
        """Ensures that Terraform state files are included in .gitignore."""
        gitignore_path = Path(self.repo.working_dir) / ".gitignore"

        ignore_block = [
            "",
            "# Opsmith",
            "# Ignore Terraform state files and directories",
            "**/.terraform/",
            "**/*.tfstate",
            "**/*.tfstate.backup",
        ]
        ignore_block_str = "\n".join(ignore_block) + "\n"

        sentinel = "**/.terraform/"

        content = ""
        if gitignore_path.exists():
            with open(gitignore_path, "r", encoding="utf-8") as f:
                content = f.read()

        if sentinel in content:
            return

        with open(gitignore_path, "a", encoding="utf-8") as f:
            f.write(ignore_block_str)

        self.events.log(STEP_SETUP, ".gitignore has been updated to ignore Terraform state files.")
