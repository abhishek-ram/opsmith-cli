"""`opsmith env list|create|status`: the environments a repository declares.

``list`` and ``status`` read the repository and nothing else - no cloud call, no terraform, no
docker - so they declare no external tools and answer on a machine that has none installed.
``create`` is the headless way to make an environment, which the interactive menu cannot be.
"""

from typing import List, Optional

import typer

from opsmith.cli import flags
from opsmith.cli.commands import requires
from opsmith.cli.output import OutputFormat
from opsmith.cli.state import CliState
from opsmith.core import operations
from opsmith.core.results import (
    EnvCreateResult,
    EnvListResult,
    EnvStatusResult,
    Resource,
)


def _rendered_list(result: EnvListResult) -> str:
    """
    Lays the environments out as a table for a terminal.

    :param result: What the operation found.
    :return: The table, as plain text.
    """
    if not result.environments:
        return "No environments yet. Create one with 'opsmith env create --name <name>'."

    rows = [("NAME", "PROVIDER", "REGION", "STRATEGY", "STATUS")]
    rows += [
        (
            environment.name,
            environment.provider,
            environment.region,
            environment.strategy,
            "deployed" if environment.deployed else "not deployed",
        )
        for environment in result.environments
    ]

    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return "\n".join(
        "  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip() for row in rows
    )


def _rendered_resource(resource: Resource) -> List[str]:
    """
    Lays one resource out for a terminal, whatever kind of thing it is.

    Nothing here knows what a virtual machine is: the headline is built from the fields every
    resource has, and whatever else the strategy reported is printed under it as it was given.
    That is what lets a strategy Opsmith has never seen render as readably as the built-in one.

    :param resource: One piece of infrastructure the environment holds.
    :return: The lines describing it.
    """
    label = resource.name or resource.id
    headline = [f"{resource.kind}:", label]
    if resource.size:
        headline.append(f"({resource.size})")

    # Some resources are addressed by the thing they are reached at - a registry is its own URL -
    # and printing it twice on one line reads as two facts when it is one.
    if resource.address and resource.address != label:
        headline.append(f"at {resource.address}")

    lines = ["  " + " ".join(headline)]
    if resource.details:
        detail = ", ".join(f"{key}={value}" for key, value in sorted(resource.details.items()))
        lines.append(f"    {detail}")
    return lines


def _rendered_status(result: EnvStatusResult) -> str:
    """
    Lays one environment out as a list of fields for a terminal.

    :param result: What the operation found.
    :return: The report, as plain text.
    """
    lines = [
        f"Environment:  {result.environment}",
        f"Provider:     {result.provider} ({result.region})",
        f"Strategy:     {result.strategy}",
        f"Status:       {'deployed' if result.deployed else 'not deployed'}",
    ]
    if result.registry_url:
        lines.append(f"Registry:     {result.registry_url}")
    if result.resources:
        lines.append("Resources:")
        for resource in result.resources:
            lines.extend(_rendered_resource(resource))
    if result.services:
        lines.append(f"Services:     {', '.join(s.name_slug for s in result.services)}")
    for slug, url in result.urls.items():
        lines.append(f"  {slug}: {url}")
    return "\n".join(lines)


def list_environments(ctx: typer.Context) -> EnvListResult:
    """List the deployment environments this repository declares."""
    state: CliState = ctx.obj
    deployment_config = operations.load_config(state.context)
    result = operations.list_environments(state.context, deployment_config)

    # The listing is this command's whole payload. In JSON mode the envelope carries it, so it
    # would be written twice; in text mode nothing else would write it at all.
    if state.output is OutputFormat.TEXT:
        state.renderer.render_document(_rendered_list(result))
    return result


def status(
    ctx: typer.Context,
    env: str = typer.Option(..., "--env", help="The environment to report on."),
) -> EnvStatusResult:
    """Report what an environment is running, without contacting the cloud."""
    state: CliState = ctx.obj
    deployment_config = operations.load_config(state.context)
    result = operations.environment_status(state.context, deployment_config, env)

    if state.output is OutputFormat.TEXT:
        state.renderer.render_document(_rendered_status(result))
    return result


@requires("docker", "terraform")
def create(
    ctx: typer.Context,
    name: Optional[str] = typer.Option(None, "--name", help="The name of the new environment."),
    provider: Optional[str] = typer.Option(
        None, "--provider", help="The cloud provider to deploy to, such as AWS or GCP."
    ),
    region: Optional[str] = typer.Option(None, "--region", help="The region to deploy into."),
    strategy: Optional[str] = typer.Option(
        None, "--strategy", help="The deployment strategy, such as Monolithic."
    ),
    project_id: Optional[str] = typer.Option(
        None, "--project-id", help="The GCP project to deploy into."
    ),
    zone: Optional[str] = typer.Option(None, "--zone", help="The GCP zone to deploy into."),
    instance_type: Optional[str] = typer.Option(
        None, "--instance-type", help="The instance type to create, instead of the one suggested."
    ),
    domain: Optional[List[str]] = typer.Option(
        None, "--domain", help="A service's domain, as slug=host. Repeatable.", metavar="SLUG=HOST"
    ),
    domain_email: Optional[str] = typer.Option(
        None, "--domain-email", help="The email SSL certificates are registered with."
    ),
    env_var: Optional[List[str]] = typer.Option(
        None, "--env-var", help="A runtime value, as KEY=VALUE. Repeatable.", metavar="KEY=VALUE"
    ),
    build_env: Optional[List[str]] = typer.Option(
        None,
        "--build-env",
        help="A frontend's build-time value, as slug:KEY=VALUE. Repeatable.",
        metavar="SLUG:KEY=VALUE",
    ),
    no_deploy: bool = typer.Option(
        False,
        "--no-deploy",
        help="Write the environment to the configuration without deploying it.",
    ),
) -> EnvCreateResult:
    """Create a deployment environment, and deploy it unless told not to."""
    state: CliState = ctx.obj
    flags.supply(
        state,
        scalars={
            "--name": name,
            "--provider": provider,
            "--region": region,
            "--strategy": strategy,
            "--project-id": project_id,
            "--zone": zone,
            "--instance-type": instance_type,
            "--domain-email": domain_email,
        },
        domains=domain,
        env_vars=env_var,
        build_envs=build_env,
    )

    deployment_config = operations.load_config(state.context)
    return operations.create_environment(state.context, deployment_config, deploy=not no_deploy)
