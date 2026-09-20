"""`opsmith release|update|run|destroy`: what can be done to an environment that exists.

Each is the subcommand half of one branch of the interactive ``deploy`` menu, and calls the same
function in ``opsmith/core/operations.py`` that the menu calls.
"""

import shlex
from typing import List, Optional

import typer

from opsmith.cli import flags
from opsmith.cli.commands import requires
from opsmith.cli.state import CliState
from opsmith.core import operations
from opsmith.core.results import DestroyResult, ReleaseResult, RunResult, UpdateResult


@requires("docker", "terraform")
def release(
    ctx: typer.Context,
    env: str = typer.Option(..., "--env", help="The environment to release to."),
    env_var: Optional[List[str]] = typer.Option(
        None, "--env-var", help="A runtime value, as KEY=VALUE. Repeatable.", metavar="KEY=VALUE"
    ),
    build_env: Optional[List[str]] = typer.Option(
        None,
        "--build-env",
        help="A frontend's build-time value, as slug:KEY=VALUE. Repeatable.",
        metavar="SLUG:KEY=VALUE",
    ),
) -> ReleaseResult:
    """Build the current code and deploy it to an environment."""
    state: CliState = ctx.obj
    flags.supply(state, env_vars=env_var, build_envs=build_env)

    deployment_config = operations.load_config(state.context)
    return operations.release(state.context, deployment_config, env)


@requires("docker", "terraform")
def update(
    ctx: typer.Context,
    env: str = typer.Option(..., "--env", help="The environment to update."),
    domain: Optional[List[str]] = typer.Option(
        None, "--domain", help="A service's domain, as slug=host. Repeatable.", metavar="SLUG=HOST"
    ),
    domain_email: Optional[str] = typer.Option(
        None, "--domain-email", help="The email SSL certificates are registered with."
    ),
    env_var: Optional[List[str]] = typer.Option(
        None, "--env-var", help="A runtime value, as KEY=VALUE. Repeatable.", metavar="KEY=VALUE"
    ),
) -> UpdateResult:
    """Reconcile a deployed environment with the configuration as it now stands."""
    state: CliState = ctx.obj
    flags.supply(
        state,
        scalars={"--domain-email": domain_email},
        domains=domain,
        env_vars=env_var,
    )

    deployment_config = operations.load_config(state.context)
    return operations.update(state.context, deployment_config, env)


def run(
    ctx: typer.Context,
    env: str = typer.Option(..., "--env", help="The environment to run the command in."),
    service: str = typer.Option(..., "--service", help="The service to run the command on."),
    command: Optional[List[str]] = typer.Argument(
        None,
        help=(
            "The command to run, after a '--'. For example: opsmith run --env dev --service api --"
            " ls -la"
        ),
    ),
) -> RunResult:
    """
    Run a one-off command on a deployed service.

    Opsmith exits with whatever the command exited with, so this can be used in a script exactly
    as the command itself would be.
    """
    state: CliState = ctx.obj
    deployment_config = operations.load_config(state.context)

    # Rejoined rather than passed through as a list because the playbook interpolates it into a
    # shell. shlex puts back the quoting the user's own shell took off.
    command_line = shlex.join(command) if command else None

    return operations.run_command(
        state.context,
        deployment_config,
        env,
        service_name_slug=service,
        command=command_line,
    )


@requires("terraform")
def destroy(
    ctx: typer.Context,
    env: str = typer.Option(..., "--env", help="The environment to destroy."),
) -> DestroyResult:
    """Destroy an environment and everything it created."""
    state: CliState = ctx.obj
    deployment_config = operations.load_config(state.context)
    return operations.destroy_environment(state.context, deployment_config, env)
