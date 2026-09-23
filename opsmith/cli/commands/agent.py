"""``opsmith agent install|uninstall|status``: the Agent Skill, in the harnesses that read it.

None of these needs a model and none of them needs docker or terraform: installing a skill is
copying files, and it is the first thing a new user does. They are the one family of commands
tagged :func:`~opsmith.cli.commands.no_model` for that reason.
"""

from typing import Optional

import typer

from opsmith.cli import agent_install
from opsmith.cli.commands import no_model
from opsmith.cli.output import OutputFormat
from opsmith.cli.state import CliState
from opsmith.core.results import (
    AgentInstallResult,
    AgentLocation,
    AgentLocationState,
    AgentStatusResult,
    AgentUninstallResult,
)

INSTALL_TARGET_HELP = (
    "Which harness to install for, required: one harness name, or 'auto' for only where a"
    " harness is already set up here. Run the command again to install for another."
)

UNINSTALL_TARGET_HELP = (
    "Which harness to remove for. Everything Opsmith installed here, when not given."
)


def _rendered_location(location: AgentLocation) -> str:
    """
    :param location: One place the skill goes.
    :return: The line describing it.
    """
    line = f"{location.label} ({location.scope}): {location.state.value} - {location.path}"
    if location.installed_version:
        line += f" [{location.installed_version}]"
    if not location.verified:
        line += "  (path not yet verified against the harness)"
    return line


def _rendered_install(result: AgentInstallResult) -> str:
    """
    :param result: What the install wrote.
    :return: The report, for a terminal.
    """
    lines = [f"Installed the {result.skill} skill, version {result.version}:"]
    lines += [f"  {_rendered_location(location)}" for location in result.installed]
    if result.agents_md:
        lines.append(f"  AGENTS.md updated: {result.agents_md}")
    if result.claude_md:
        lines.append(f"  CLAUDE.md now imports it: {result.claude_md}")
    return "\n".join(lines)


def _rendered_uninstall(result: AgentUninstallResult) -> str:
    """
    :param result: What the uninstall removed.
    :return: The report, for a terminal.
    """
    if not result.removed and not result.agents_md and not result.claude_md:
        return "Nothing to remove."

    lines = ["Removed:"]
    # The state of a location that has just been emptied is "missing", which is true and reads
    # like a complaint, so a removal reports where it was and nothing else.
    lines += [
        f"  {location.label} ({location.scope}): {location.path}" for location in result.removed
    ]
    if result.agents_md:
        lines.append(f"  the Opsmith block in {result.agents_md}")
    if result.claude_md:
        lines.append(f"  the import line in {result.claude_md}")
    return "\n".join(lines)


def _rendered_status(result: AgentStatusResult) -> str:
    """
    :param result: Where the skill is, and whether it is current.
    :return: The report, for a terminal.
    """
    lines = [
        f"Opsmith {result.package_version}, skill {result.skill} {result.skill_version}.",
        "",
    ]
    # A location for a harness that is not set up here at all is noise, not news.
    reportable = [
        location
        for location in result.locations
        if location.state is not AgentLocationState.NOT_APPLICABLE
    ]
    lines += [_rendered_location(location) for location in reportable]

    if not reportable:
        lines.append(
            "None of the harnesses Opsmith knows about is set up here. Run 'opsmith agent"
            " install --target <harness>' to write the skill for one anyway."
        )
    if result.agents_md:
        lines.append(f"AGENTS.md carries the Opsmith block: {result.agents_md}")
    return "\n".join(lines)


@no_model
def install(
    ctx: typer.Context,
    target: Optional[str] = typer.Option(None, "--target", help=INSTALL_TARGET_HELP),
    scope: str = typer.Option(
        agent_install.PROJECT_SCOPE,
        "--scope",
        help="Whether to install into this project or for this user.",
    ),
    agents_md: bool = typer.Option(
        False,
        "--agents-md",
        help="Also write the Opsmith block into AGENTS.md, and the import line into CLAUDE.md.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Replace a skill directory that does not identify itself as Opsmith's.",
    ),
    mcp: bool = typer.Option(
        False, "--mcp", help="Reserved for MCP configuration, which is not written yet."
    ),
) -> AgentInstallResult:
    """Install the Opsmith skill into the harnesses that read skills."""
    state: CliState = ctx.obj
    result = agent_install.install(
        state.context,
        target=target,
        scope=scope,
        agents_md=agents_md,
        force=force,
        mcp=mcp,
    )

    if state.output is OutputFormat.TEXT:
        state.renderer.render_document(_rendered_install(result))
    return result


@no_model
def uninstall(
    ctx: typer.Context,
    target: Optional[str] = typer.Option(None, "--target", help=UNINSTALL_TARGET_HELP),
    scope: str = typer.Option(
        agent_install.PROJECT_SCOPE,
        "--scope",
        help="Whether to remove from this project or from this user's directories.",
    ),
) -> AgentUninstallResult:
    """Remove the Opsmith skill, and only what installing it wrote."""
    state: CliState = ctx.obj
    result = agent_install.uninstall(state.context, target=target, scope=scope)

    if state.output is OutputFormat.TEXT:
        state.renderer.render_document(_rendered_uninstall(result))
    return result


@no_model
def status(
    ctx: typer.Context,
    scope: Optional[str] = typer.Option(
        None, "--scope", help="Report on one scope only. Both when not given."
    ),
) -> AgentStatusResult:
    """Report where the skill is installed, and whether it is the version running."""
    state: CliState = ctx.obj
    result = agent_install.status(state.context, scope=scope)

    if state.output is OutputFormat.TEXT:
        state.renderer.render_document(_rendered_status(result))
    return result
