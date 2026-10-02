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


def _commit_everything(tmp_project: Path, message: str = "commit") -> None:
    """
    Stages every file in the project and commits it, with an identity given here rather than read
    from the machine's git configuration, which a CI runner may not have.
    """
    repo = git.Repo(str(tmp_project))
    repo.git.add("--all")
    actor = git.Actor("Opsmith Test", "test@opsmith.invalid")
    repo.index.commit(message, author=actor, committer=actor)


def test_a_clean_tree_has_no_uncommitted_changes(tmp_project: Path):
    """A repository whose working tree matches its last commit reports nothing."""
    (tmp_project / "app.py").write_text("print('v1')\n")
    _commit_everything(tmp_project)

    assert GitRepo(tmp_project).uncommitted_changes() == []


def test_uncommitted_changes_lists_every_kind_of_change(tmp_project: Path):
    """
    A modified file, a staged new file and an untracked file are all left out of a build taken
    from HEAD, so all three are reported, by their path from the repository root.
    """
    (tmp_project / "app.py").write_text("print('v1')\n")
    _commit_everything(tmp_project)

    (tmp_project / "app.py").write_text("print('v2')\n")
    (tmp_project / "staged.py").write_text("print('staged')\n")
    git.Repo(str(tmp_project)).git.add("staged.py")
    (tmp_project / "untracked.py").write_text("print('untracked')\n")

    changed = GitRepo(tmp_project).uncommitted_changes()

    assert sorted(changed) == ["app.py", "staged.py", "untracked.py"]


def test_a_rename_is_reported_once_by_its_new_path(tmp_project: Path):
    """
    With -z, git follows a rename with the path it came from as an entry of its own. That entry is
    not a change, and reading it as one would report a file that no longer exists.
    """
    (tmp_project / "old_name.py").write_text("print('renamed')\n")
    _commit_everything(tmp_project)

    git.Repo(str(tmp_project)).git.mv("old_name.py", "new_name.py")

    assert GitRepo(tmp_project).uncommitted_changes() == ["new_name.py"]


def test_changes_under_the_excluded_directory_are_not_reported(tmp_project: Path):
    """
    Opsmith writes under .opsmith/ on every run, so a build that asked about it would report its own
    working directories. Excluding it leaves only the changes that matter to the images.
    """
    (tmp_project / "app.py").write_text("print('v1')\n")
    (tmp_project / ".opsmith" / "deployments.yml").write_text("app_name: test\n")
    _commit_everything(tmp_project)

    (tmp_project / ".opsmith" / "deployments.yml").write_text("app_name: changed\n")
    (tmp_project / ".opsmith" / "environments").mkdir()
    (tmp_project / ".opsmith" / "environments" / "state.yml").write_text("{}\n")
    (tmp_project / "app.py").write_text("print('v2')\n")

    changed = GitRepo(tmp_project).uncommitted_changes(excluding=tmp_project / ".opsmith")

    assert changed == ["app.py"]


def test_the_build_context_is_the_last_commit_not_the_working_tree(tmp_project: Path):
    """
    The archive a build runs in is taken from HEAD: an uncommitted edit is not in it and neither
    is an untracked file. This is the behaviour the skill tells an agent about, and the reason
    uncommitted changes are reported at all.
    """
    (tmp_project / "app.py").write_text("print('v1')\n")
    _commit_everything(tmp_project)

    (tmp_project / "app.py").write_text("print('v2')\n")
    (tmp_project / "untracked.py").write_text("print('untracked')\n")

    with GitRepo(tmp_project).git_archive_context() as context_path:
        assert (context_path / "app.py").read_text() == "print('v1')\n"
        assert not (context_path / "untracked.py").exists()
