"""The `setup` command: detect services and write the deployment configuration."""

from typing import Any, Callable, List, Tuple

import typer
import yaml
from rich import print

from opsmith.cli.commands import requires
from opsmith.cli.state import CliState
from opsmith.core.config import ConfigIssue, parse_infra_deps, parse_service
from opsmith.core.interaction import Choice, Interaction
from opsmith.service_detector import ServiceDetector
from opsmith.types import DeploymentConfig
from opsmith.utils import slugify


def _report_issues(interact: Interaction, heading: str, issues: List[ConfigIssue]):
    """
    Shows the problems found in an edited document, so the editor can be reopened on them.

    :param interact: How the run reaches the user.
    :param heading: What was being edited.
    :param issues: The problems found in it.
    """
    for issue in issues:
        location = f"{issue.path}: " if issue.path else ""
        interact.notify(f"{heading}: {location}{issue.message}", details=issue.model_dump())


def _review(
    interact: Interaction,
    key: str,
    message: str,
    heading: str,
    document: str,
    parse: Callable[[str], Tuple[Any, List[ConfigIssue]]],
) -> Any:
    """
    Hands a proposed document to the user and reopens the editor until it parses.

    The reopening used to be inquirer's, driven by a validator that printed as a side effect.
    It is a plain loop now, because the same rules have to hold for a run with nobody at the
    keyboard, and only the caller knows what to do with what comes back.

    The loop terminates with nobody there too: a headless review editor accepts a proposal once,
    and refuses the same key a second time, because being asked again means what it accepted did
    not parse and accepting it again would only not parse again.

    :param interact: How the run reaches the user.
    :param key: The interaction key the edit is addressed by.
    :param message: What the user is being asked to review.
    :param heading: How to label a problem found in what they saved.
    :param document: The proposal, as YAML.
    :param parse: Turns the edited YAML into the object, or into the issues that stopped it.
    :return: Whatever ``parse`` produced once it produced something.
    """
    while True:
        document = interact.edit(key, message, content=document, on_headless="accept")
        parsed, issues = parse(document)
        if parsed is not None:
            return parsed
        _report_issues(interact, heading, issues)


@requires("docker", "terraform")
def setup(ctx: typer.Context):
    """
    Setup the deployment configuration for the repository.
    Identifies services, their languages, types, and frameworks.
    """
    state: CliState = ctx.obj
    interact = state.context.interact
    detector = ServiceDetector(ctx=state.context)
    deployment_config = DeploymentConfig.load(state.deployments_path)
    scan_services = False

    if deployment_config:
        print("\n[bold yellow]Existing deployment configuration found.[/bold yellow]")

        update_actions = [
            Choice(label="Re-scan services", value="rescan"),
            Choice(label="Exit", value="exit"),
        ]
        action = interact.select(
            "setup.action", "What would you like to do?", update_actions, default="exit"
        )
        if action == "exit":
            print("Exiting setup.")
            return

        scan_services = True

    else:
        print("No existing deployment configuration found. Starting analysis...\n")
        app_name = interact.ask("app.name", "Enter the application name")
        if not app_name:
            print("[bold red]Application name is required. Aborting.[/bold red]")
            raise typer.Exit(code=1)

        deployment_config = DeploymentConfig(
            app_name=app_name,
            app_name_slug=slugify(app_name),
        )
        scan_services = True
        state.context.git_repo.ensure_gitignore()

    if scan_services:
        print("Scanning your codebase now to detect services, frameworks, and languages...")
        service_list_obj = detector.detect_services(existing_config=deployment_config)

        confirmed_services = []

        if service_list_obj.services:
            print("\n[bold]Please review and confirm each detected service:[/bold]")

        for i, service in enumerate(service_list_obj.services):
            service_yaml = yaml.dump(service.model_dump(mode="json"), indent=2)

            editor_prompt_message = (
                f"Review and confirm Service {i + 1}/{len(service_list_obj.services)}"
            )
            confirmed_service = _review(
                interact,
                f"service.{service.name_slug}.confirm",
                editor_prompt_message,
                "Invalid service configuration",
                service_yaml,
                parse_service,
            )
            confirmed_services.append(confirmed_service)

            print("\n[bold blue]Generating Dockerfile for the updated service...[/bold blue]")
            detector.generate_dockerfile(service=confirmed_service)

        deployment_config.services = confirmed_services

        infra_deps = service_list_obj.infra_deps
        if infra_deps:
            print("\n[bold]Please review and confirm detected infrastructure dependencies.[/bold]")
            deps_yaml = yaml.dump([dep.model_dump(mode="json") for dep in infra_deps], indent=2)
            editor_prompt_message = (
                "Review and confirm dependencies.\nIf 'provider' is 'user_choice', please replace"
                " it with a valid provider.\nEach provider can only be listed once."
            )
            deployment_config.infra_deps = _review(
                interact,
                "infra_deps.confirm",
                editor_prompt_message,
                "Invalid dependency configuration",
                deps_yaml,
                parse_infra_deps,
            )

    # Create/Update and Save Configuration
    config_path = deployment_config.save(state.deployments_path)
    print(f"\n[bold blue]Deployment configuration saved to: {config_path}[/bold blue]")
