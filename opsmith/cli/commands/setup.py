"""The `setup` command: detect services and write the deployment configuration."""

import inquirer
import typer
import yaml
from pydantic import ValidationError
from rich import print

from opsmith.cli.state import CliState
from opsmith.git_repo import GitRepo
from opsmith.service_detector import ServiceDetector
from opsmith.types import DeploymentConfig, InfrastructureDependency, ServiceInfo
from opsmith.utils import slugify


def _validate_service_config(_, config_yaml: str) -> bool:
    try:
        data = yaml.safe_load(config_yaml)
        ServiceInfo(**data)
        return True
    except (yaml.YAMLError, ValidationError) as e:
        print(f"\n[red]>>[/red] Invalid service configuration: {e}\n")
        return False


def _validate_infra_deps_config(_, config_yaml: str) -> bool:
    try:
        if "user_choice" in config_yaml:
            raise ValueError(
                "Provider is 'user_choice'. Please replace it with a valid provider.",
            )

        data = yaml.safe_load(config_yaml)
        if not isinstance(data, list):
            raise ValueError("Configuration must be a YAML list of dependencies.")

        deps = [InfrastructureDependency(**item) for item in data]

        seen_providers = set()
        for dep in deps:
            if dep.provider in seen_providers:
                print("Duplicate provider")
                raise ValueError(
                    f"Duplicate provider found: {dep.provider}. Each provider can only be"
                    " listed once."
                )
            seen_providers.add(dep.provider)
        return True
    except (yaml.YAMLError, ValidationError, ValueError) as e:
        print(f"\n[red]>>[/red] Invalid dependency configuration: {e}\n")
        return False


def setup(ctx: typer.Context):
    """
    Setup the deployment configuration for the repository.
    Identifies services, their languages, types, and frameworks.
    """
    state: CliState = ctx.obj
    detector = ServiceDetector(src_dir=state.src_dir, agent=state.agent, verbose=state.verbose)
    git_repo = GitRepo(state.src_dir)
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
        git_repo.ensure_gitignore()

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
            confirmed_service_data = yaml.safe_load(answers["config"])
            confirmed_service = ServiceInfo(**confirmed_service_data)
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
            confirmed_deps_data = yaml.safe_load(answers["config"])
            deployment_config.infra_deps = [
                InfrastructureDependency(**data) for data in confirmed_deps_data
            ]

    # Create/Update and Save Configuration
    deployment_config.save(state.deployments_path)
