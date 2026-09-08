"""Tests for GitRepo, which raises an OpsmithError instead of exiting the process."""

from pathlib import Path

import git
import pytest

from opsmith.core.errors import NotAGitRepository
from opsmith.git_repo import GitRepo


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
