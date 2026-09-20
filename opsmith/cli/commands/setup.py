"""The `setup` and `init` commands: write the deployment configuration for a repository.

Both are thin. What they do lives in ``opsmith/core/operations.py``, so the same detection and the
same editors run whether a person invoked them or a harness did.
"""

import typer

from opsmith.cli import flags
from opsmith.cli.commands import requires
from opsmith.cli.state import CliState
from opsmith.core import operations
from opsmith.core.results import InitResult, SetupResult


def init(
    ctx: typer.Context,
    app_name: str = typer.Option(
        None,
        "--app-name",
        help="The name of the application. Asked for when it is not given.",
    ),
) -> InitResult:
    """Create a deployment configuration for this repository, without scanning it."""
    state: CliState = ctx.obj
    flags.supply(state, scalars={"--app-name": app_name})
    return operations.init_config(state.context)


@requires("docker", "terraform")
def setup(
    ctx: typer.Context,
    rescan: bool = typer.Option(
        False,
        "--rescan",
        help="Re-scan a repository that already has a configuration, instead of leaving it alone.",
    ),
    accept_detected: bool = typer.Option(
        False,
        "--accept-detected",
        help=(
            "Take the detected services and dependencies as they are, without opening an editor"
            " to review each one."
        ),
    ),
) -> SetupResult:
    """
    Setup the deployment configuration for the repository.
    Identifies services, their languages, types, and frameworks.
    """
    state: CliState = ctx.obj
    flags.supply(state, rescan=rescan, accept_reviews=accept_detected)
    return operations.run_setup(state.context)
