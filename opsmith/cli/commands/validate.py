"""``opsmith dockerfile validate``: build a service's Dockerfile, run it, and say what happened.

This is the validator half of the bargain with a coding harness. The harness explores the
repository and writes the Dockerfile with its own model; Opsmith builds it, runs it and reports.
Nothing here generates or repairs anything - the caller wrote the file, so the caller fixes it.

The compose commands of phase 2 join this module, which is why it is named for validating rather
than for Dockerfiles.
"""

from typing import List, Optional

import typer

from opsmith.cli.commands import requires
from opsmith.cli.output import OutputFormat
from opsmith.cli.state import CliState
from opsmith.core import operations
from opsmith.core.results import DockerfileCheck, DockerfileValidateResult


def _rendered_check(check: DockerfileCheck) -> List[str]:
    """
    :param check: What building and running one service's Dockerfile did.
    :return: The lines describing it, for a terminal.
    """
    lines = [f"{check.service}: {'ok' if check.ok else 'not usable'}", f"  {check.dockerfile}"]
    lines.append(f"  build: {'ok' if check.build_ok else 'failed'}")

    if check.run_ok is None:
        lines.append("  run: not attempted, because the build failed")
    elif check.run_timed_out:
        lines.append("  run: still up when the watch ended, which counts as healthy")
    else:
        lines.append(f"  run: {'ok' if check.run_ok else 'exited non-zero'}")

    if check.explanation:
        lines.append(f"  {check.explanation}")
    return lines


def _rendered_checks(result: DockerfileValidateResult) -> str:
    """
    :param result: What the validation found.
    :return: The report, for a terminal.
    """
    lines: List[str] = []
    for check in result.checks:
        lines += _rendered_check(check)
    return "\n".join(lines)


@requires("docker")
def validate_dockerfile(
    ctx: typer.Context,
    service: Optional[str] = typer.Option(
        None,
        "--service",
        help="The service to check. Every service built from a Dockerfile when not given.",
    ),
    timeout: Optional[int] = typer.Option(
        None,
        "--timeout",
        help="Seconds to let the container run before counting it as healthy. Default 60.",
    ),
) -> DockerfileValidateResult:
    """Build and run the Dockerfiles this repository declares, without changing them."""
    state: CliState = ctx.obj
    deployment_config = operations.load_config(state.context)
    result = operations.validate_dockerfiles(
        state.context,
        deployment_config,
        service_name_slug=service,
        run_timeout_s=timeout,
    )

    if state.output is OutputFormat.TEXT:
        state.renderer.render_document(_rendered_checks(result))
    return result
