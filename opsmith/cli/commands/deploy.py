"""The `deploy` command: the interactive environment menu.

It is a dispatcher and nothing else. Every branch below resolves a choice to one function in
``opsmith/core/operations.py`` and calls it - the same function the matching subcommand calls,
with the same arguments. That is the rule that stops the menu and the headless surface drifting
apart, and it is why nothing here names a strategy or a provisioner.

Creating an environment is still a terminal affair: the menu asks ``env.name`` twice under one
key - once to choose among the environments that exist, once to name a new one - and no single
answer satisfies both. A run with nobody at the keyboard stops on the first and is told to use
``opsmith env create``, which is the headless way in.
"""

from typing import Union

import typer

from opsmith.cli.commands import requires
from opsmith.cli.state import CliState
from opsmith.core import operations
from opsmith.core.errors import MissingAnswerError
from opsmith.core.interaction import Choice
from opsmith.core.results import (
    DestroyResult,
    EnvCreateResult,
    ReleaseResult,
    RunResult,
    UpdateResult,
)

#: What the menu can be asked to do with an environment that already exists.
ACTIONS = ["release", "update", "run", "delete", "exit"]

#: What a run with nobody at the keyboard is told when it stops on the menu's first question.
HEADLESS_HINT = (
    "'deploy' is an interactive menu. Use 'opsmith env create --name <name> --provider <provider>"
    " --strategy <strategy>' to create an environment, or 'opsmith release|update|run|destroy"
    " --env <name>' to act on one."
)


@requires("docker", "terraform")
def deploy(
    ctx: typer.Context,
) -> Union[EnvCreateResult, ReleaseResult, UpdateResult, RunResult, DestroyResult, None]:
    """Deploy the application to a specified environment."""
    state: CliState = ctx.obj
    context = state.context
    deployment_config = operations.load_config(context)

    try:
        selected = operations.select_environment(context, deployment_config)
    except MissingAnswerError as err:
        # The menu is the one flow a driver cannot complete, so the stop says so rather than
        # asking for an answer that would only be ambiguous if it arrived.
        err.hint = HEADLESS_HINT
        err.details["subcommands"] = ["env create", "release", "update", "run", "destroy"]
        raise

    if selected == operations.CREATE_NEW_ENVIRONMENT:
        return operations.create_environment(context, deployment_config)

    operations.bind_environment(context, selected)
    action = context.interact.select(
        "env.action",
        f"What would you like to do with the '{selected}' environment?",
        [Choice(label=action, value=action) for action in ACTIONS],
        default="release",
    )

    if action == "exit":
        context.interact.notify("Exiting deploy.")
        return None
    if action == "release":
        return operations.release(context, deployment_config, selected)
    if action == "update":
        return operations.update(context, deployment_config, selected)
    if action == "run":
        return operations.run_command(context, deployment_config, selected)
    return operations.destroy_environment(context, deployment_config, selected)
