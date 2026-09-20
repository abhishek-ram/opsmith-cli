"""Tests for GitRepo, which raises an OpsmithError instead of exiting the process."""

import subprocess
import sys
from pathlib import Path

import git
import pytest

from opsmith.core.errors import GitNotAvailable, NotAGitRepository
from opsmith.git_repo import GitRepo, _import_git

#: The directory holding the ``opsmith`` package, for a subprocess that must import it fresh.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent


def test_directory_outside_a_repository_raises(tmp_path: Path):
    """
    Constructing a GitRepo on a directory that is not inside a git repository raises
    NotAGitRepository, so library code no longer terminates the process on its own.
    """
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()

    with pytest.raises(NotAGitRepository) as raised:
        GitRepo(plain_dir)

    assert raised.value.code == "INVALID_ARGUMENT"
    assert raised.value.details == {"src_dir": str(plain_dir)}


def test_repository_directory_constructs(tmp_project: Path):
    """A directory that is a git repository yields a usable GitRepo."""
    repo = GitRepo(tmp_project)

    assert Path(repo.repo.working_dir) == tmp_project.resolve()


def test_subdirectory_of_a_repository_constructs(tmp_project: Path):
    """
    A subdirectory of a repository resolves upwards to the repository root, which is what
    lets opsmith run from anywhere inside a project.
    """
    nested = tmp_project / "services" / "api"
    nested.mkdir(parents=True)

    repo = GitRepo(nested)

    assert Path(repo.repo.working_dir) == tmp_project.resolve()


def test_ensure_gitignore_is_idempotent(tmp_project: Path):
    """Calling ensure_gitignore twice leaves exactly one opsmith block in .gitignore."""
    repo = GitRepo(tmp_project)

    repo.ensure_gitignore()
    repo.ensure_gitignore()

    content = (tmp_project / ".gitignore").read_text()
    assert content.count("**/.terraform/") == 1


def test_git_repo_does_not_import_typer():
    """
    GitRepo used to raise typer.Exit. The module must no longer reach for typer at all, so
    it can be used from a library context.
    """
    import opsmith.git_repo as git_repo_module

    assert "typer" not in dir(git_repo_module)
    assert git.Repo is not None


def test_importing_opsmith_does_not_need_git(tmp_path: Path):
    """
    GitPython looks for the git executable while it is being imported, so importing it at module
    scope would make importing any part of Opsmith fail on a machine without git - long before
    anything asked for a repository. It is imported where a repository is opened instead, and this
    proves it in a fresh interpreter with an empty PATH, which is the only place it can be proved:
    this one imported git long ago.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys\nimport opsmith.main\nprint('git' in sys.modules)\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(probe)],
        env={"PATH": "", "HOME": str(tmp_path), "PYTHONPATH": str(PACKAGE_ROOT)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", "importing opsmith pulled in GitPython"


def test_a_missing_git_executable_is_a_usage_error_naming_git(monkeypatch):
    """
    Without git there is no repository to open, and saying so is the whole point of deferring the
    import: it is reported as a usage error naming git, not as an unhandled crash and not as the
    'run git init' message, which would send somebody to fix the wrong thing.

    ``None`` in sys.modules is what makes the import fail the way it fails on a machine with no
    git, which this machine has.
    """
    monkeypatch.setitem(sys.modules, "git", None)

    with pytest.raises(GitNotAvailable) as raised:
        _import_git()

    assert "not on the PATH" in raised.value.message
    assert not isinstance(raised.value, NotAGitRepository)


class _GitThatWillNotRun:
    """Stands in for GitPython's command wrapper once the executable has gone."""

    def ls_files(self, *args, **kwargs):
        """Fails the way GitPython does when it cannot run git."""
        raise git.exc.GitCommandNotFound("git", "not found")


def test_a_git_that_cannot_be_run_is_reported_rather_than_crashing(tmp_project: Path):
    """
    A repository opens by reading the files under .git, so it opens on a machine with no git at
    all; the executable is not needed until something asks git to do something. That call used to
    raise straight through the CLI and be reported as a bug in Opsmith.
    """
    repo = GitRepo(tmp_project)

    class _RepoWithNoExecutable:
        """The opened repository, with the one call that shells out replaced."""

        working_dir = str(tmp_project)
        git = _GitThatWillNotRun()

    repo.repo = _RepoWithNoExecutable()

    with pytest.raises(GitNotAvailable) as raised:
        repo.get_git_tracked_files(["."])

    assert "could not be run" in raised.value.message
