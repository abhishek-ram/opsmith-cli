"""The `setup` command: detect services and write the deployment configuration."""

from typing import List

import inquirer
import typer
import yaml
from rich import print

from opsmith.cli.commands import requires
from opsmith.cli.state import CliState
from opsmith.core.config import ConfigIssue, parse_infra_deps, parse_service
from opsmith.core.errors import InvalidConfig
from opsmith.service_detector import ServiceDetector
from opsmith.types import DeploymentConfig
from opsmith.utils import slugify


def _report_issues(heading: str, issues: List[ConfigIssue]):
    """
    Shows the problems found in an edited document, so the editor can be reopened on them.

    :param heading: What was being edited.
    :param issues: The problems found in it.
    """
    for issue in issues:
        location = f"{issue.path}: " if issue.path else ""
        print(f"\n[red]>>[/red] {heading}: {location}{issue.message}\n")


def _validate_service_config(_, config_yaml: str) -> bool:
    """
    Validates an edited service, in the ``(answers, value) -> bool`` shape inquirer expects.

    :param config_yaml: The YAML the user edited.
    :return: Whether it is usable, which is what decides if inquirer reopens the editor.
    """
    _, issues = parse_service(config_yaml)
    _report_issues("Invalid service configuration", issues)
    return not issues


def _validate_infra_deps_config(_, config_yaml: str) -> bool:
    """
    Validates the edited dependency list, in the shape inquirer expects.

    :param config_yaml: The YAML the user edited.
    :return: Whether it is usable, which is what decides if inquirer reopens the editor.
    """
    _, issues = parse_infra_deps(config_yaml)
    _report_issues("Invalid dependency configuration", issues)
    return not issues


@requires("docker", "terraform")
def setup(ctx: typer.Context):
    """
    Setup the deployment configuration for the repository.
    Identifies services, their languages, types, and frameworks.
    """
    state: CliState = ctx.obj
    detector = ServiceDetector(ctx=state.context)
    deployment_config = DeploymentConfig.load(state.deployments_path)
    scan_services = False

    if deployment_config:
        print("\n[bold yellow]Existing deployment configuration found.[/bold yellow]")

        update_actions = ["Re-scan services", "Exit"]
        questions = [
            inquirer.List(
                "action",
                message="What would you like to do?",
                choices=update_actions,
                default="Exit",
            )
        ]
        answers = inquirer.prompt(questions)
        if not answers or answers.get("action") == "Exit":
            print("Exiting setup.")
            return

        if answers.get("action") == "Re-scan services":
            scan_services = True

    else:
        print("No existing deployment configuration found. Starting analysis...\n")
        app_name_questions = [
            inquirer.Text("app_name", message="Enter the application name"),
        ]
        app_name_answers = inquirer.prompt(app_name_questions)
        if not app_name_answers or not app_name_answers.get("app_name"):
            print("[bold red]Application name is required. Aborting.[/bold red]")
            raise typer.Exit(code=1)
        app_name = app_name_answers["app_name"]

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
            questions = [
                inquirer.Editor(
                    "config",
                    message=editor_prompt_message,
                    default=service_yaml,
                    validate=_validate_service_config,
                )
            ]
            answers = inquirer.prompt(questions)
            confirmed_service, issues = parse_service(answers["config"])
            if confirmed_service is None:
                raise InvalidConfig(
                    "The edited service configuration is not usable.",
                    details={"errors": [issue.model_dump() for issue in issues]},
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
            questions = [
                inquirer.Editor(
                    "config",
                    message=editor_prompt_message,
                    default=deps_yaml,
                    validate=_validate_infra_deps_config,
                )
            ]
            answers = inquirer.prompt(questions)
            confirmed_deps, issues = parse_infra_deps(answers["config"])
            if confirmed_deps is None:
                raise InvalidConfig(
                    "The edited dependency configuration is not usable.",
                    details={"errors": [issue.model_dump() for issue in issues]},
                )
            deployment_config.infra_deps = confirmed_deps

    # Create/Update and Save Configuration
    config_path = deployment_config.save(state.deployments_path)
    print(f"\n[bold blue]Deployment configuration saved to: {config_path}[/bold blue]")
