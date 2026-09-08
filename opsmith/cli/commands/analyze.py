"""The `repomap` command: print a map of the repository. Phase 5 replaces it."""

from typing import Dict, Optional

import typer
from rich import print

from opsmith.cli.output import OutputFormat
from opsmith.cli.state import CliState
from opsmith.repo_map import RepoMap


def repomap(ctx: typer.Context) -> Optional[Dict]:
    """
    Generates a map of the repository, showing important files and code elements.
    """
    state: CliState = ctx.obj
    print("Generating repo map now...")

    repo_mapper = RepoMap(ctx=state.context)
    repo_map_str = repo_mapper.get_repo_map()

    # The map is this command's payload, so in JSON mode it belongs in the envelope rather
    # than loose on stdout next to it.
    if state.output is OutputFormat.JSON:
        return {"repo_map": repo_map_str or ""}

    if repo_map_str:
        typer.echo(repo_map_str)
    else:
        typer.echo("No git-tracked files found in this repository or failed to generate map.")
    return None
