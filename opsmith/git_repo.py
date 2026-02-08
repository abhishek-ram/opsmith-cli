import io
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import List

import git
import typer
from rich import print


class GitRepo:
    def __init__(self, root_dir: Path):
        """
        Initialize a Git repository object for the specified directory, or its parent directories
        if it is a subdirectory, ensuring it belongs to an actual Git repository. If the provided
        directory is not within a Git repository, it exits the program with an error.

        :param root_dir: The root directory or subdirectory intended for Git repository initialization.
        :type root_dir: Path

        :raises git.exc.InvalidGitRepositoryError: If the specified directory or its parent directories
            do not contain a valid Git repository.
        :raises typer.Exit: Exits the program if the directory is not part of a valid Git repository.
        """
        try:
            # Initialize repo object, searching upwards from root_dir if it's a subdirectory
            self.repo = git.Repo(str(root_dir), search_parent_directories=True)

        except git.exc.InvalidGitRepositoryError:
            print("[bold red]Not a git repository or git is not found in PATH[/bold red].")
            raise typer.Exit()

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
        print("Creating build context from git-tracked files...")
        with tempfile.TemporaryDirectory() as temp_dir:
            buf = io.BytesIO()
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

        print(
            "[bold green].gitignore has been updated to ignore Terraform state files.\n[/bold"
            " green]"
        )
