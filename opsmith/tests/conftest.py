"""Shared fixtures for the Opsmith test suite."""

from pathlib import Path

import git
import pytest
import rich
from typer.testing import CliRunner


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
