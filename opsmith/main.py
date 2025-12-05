import os
from pathlib import Path
from typing import Annotated, List, Optional, Tuple, Type, Union

import inquirer
import logfire
import typer
import yaml
from pydantic import ValidationError
from rich import print

from opsmith.agent import build_agent
from opsmith.cloud_providers import CLOUD_PROVIDER_REGISTRY
from opsmith.cloud_providers.base import CloudCredentialsError
from opsmith.deployment_strategies import DEPLOYMENT_STRATEGY_REGISTRY
from opsmith.git_repo import GitRepo
from opsmith.models import MODEL_REGISTRY, BaseAiModel
from opsmith.repo_map import RepoMap
from opsmith.service_detector import ServiceDetector
from opsmith.settings import settings
from opsmith.types import (
    DeploymentConfig,
    DeploymentEnvironment,
    DomainInfo,
    InfrastructureDependency,
    ServiceInfo,
    ServiceTypeEnum,
)
from opsmith.utils import build_logo, get_missing_external_dependencies, slugify

app = typer.Typer(pretty_exceptions_show_locals=False)


def _check_external_dependencies():
    """
    Checks if a list of external command-line tools are installed and operational.
    Exits the application if any dependency is not found or non-operational.

    """
    missing_deps = get_missing_external_dependencies(["docker", "terraform"])
    if missing_deps:
        print(
            "[red]Required dependencies not found or not running:[/red] [bold"
            f" red]{', '.join(missing_deps)}[/bold red]"
        )
        print("[red]Please install them and ensure they are in your system's PATH.[/red]")
        raise typer.Exit(code=1)


def _parse_model_arg(model: Union[str, BaseAiModel]) -> BaseAiModel:
    """
    Fetches the class corresponding to the given model name.

    Attempts to retrieve the class for the provided model from
    the model registry. If the model name is not found in
    the registry, an error is raised indicating that the model is unsupported.

    :param model: The name of the model for which the class is required.
    :type model: str

    :return: The class corresponding to the provided model name.
    :rtype: Type[BaseAiModel]

    :raises ValueError: If the given model name is not found in the model registry.
    :raises typer.BadParameter: If the provided model name is unsupported.
    """
    if isinstance(model, BaseAiModel):
        return model
    try:
        return MODEL_REGISTRY.get_model_class(model)()
    except ValueError:
        raise typer.BadParameter(
            f"Unsupported model name: {model}, must be one of: {MODEL_REGISTRY.model_names}"
        )


def _api_key_callback(ctx: typer.Context, value: str):
    """
    This function serves as a callback for validating and processing an API key when
    used in conjunction with a command-line interface. The function checks whether
    the mandatory `--model` option is set before associating it with the provided
    API key. If validation passes, it ensures the API key authentication process
    is triggered for the specified model configuration.

    Raises a BadParameter error if `--model` was not supplied before `--api-key`.

    :param ctx: The Typer context object that contains information about the
        current command execution context, including provided options and other
        runtime parameters.
    :type ctx: typer.Context
    :param value: The API key provided by the user via the `--api-key` option
        during the command-line execution.
    :type value: str
    :return: The validated API key after ensuring it is associated with the
        specified model configuration.
    :rtype: str
    """
    if "model" not in ctx.params:
        raise typer.BadParameter("The --model option must be specified before --api-key.")
    model_class = ctx.params["model"]
    model_class.ensure_auth(value)
    return value


@app.callback()
def main(
    ctx: typer.Context,
    model: Annotated[
        Type[BaseAiModel],
        typer.Option(
            parser=_parse_model_arg,
            help="The LLM model to be used for by the AI Agent.",
            prompt="Select the LLM model to be used for by the AI Agent",
        ),
    ],
    api_key: Annotated[
        str,
        typer.Option(
            callback=_api_key_callback,
            help=(
                "The API KEY to be used for by the AI Agent. This is the API key for the specified"
                " model."
            ),
            prompt="Enter the API KEY for the specified model",
            hide_input=True,
        ),
    ],
    logfire_token: Optional[str] = typer.Option(
        default=None,
        help=(
            "Logfire token to be used for logging. If not provided, logs will not be sent to"
            " Logfire."
        ),
    ),
    src_dir: Optional[str] = typer.Option(
        default=None,
        help="Source directory to be used by the command. Defaults to current working directory.",
        envvar="OPSMITH_SRC_DIR",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable verbose output for repo map generation."
    ),
):
    """
    AI Devops engineer in your terminal.
    """
    print(build_logo())

    if logfire_token:
        logfire.configure(token=logfire_token, scrubbing=False)

    src_dir = src_dir or os.getcwd()
    ctx.obj = {
        "src_dir": Path(src_dir),
        "deployments_path": Path(src_dir).joinpath(settings.deployments_dir),
        "agent": build_agent(model_config=model, instrument=bool(logfire_token)),
    }
    _check_external_dependencies()


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


@app.command()
def setup(ctx: typer.Context):
    """
    Setup the deployment configuration for the repository.
    Identifies services, their languages, types, and frameworks.
    """
    detector = ServiceDetector(src_dir=ctx.obj["src_dir"], agent=ctx.obj["agent"])
    git_repo = GitRepo(Path(ctx.obj["src_dir"]))
    deployment_config = DeploymentConfig.load(ctx.obj["deployments_path"])
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
    deployment_config.save(ctx.obj["deployments_path"])


def _collect_domain_configuration(
    deployment_config: DeploymentConfig,
    selected_env: Optional[DeploymentEnvironment] = None,
) -> Tuple[Optional[str], List[DomainInfo]]:
    """
    Collects domain information for services that require domains.

    Returns:
        Tuple of (domain_email, list of DomainInfo objects)
    """
    services_with_domains_types = [
        s
        for s in deployment_config.services
        if s.service_type
        in [
            ServiceTypeEnum.BACKEND_API,
            ServiceTypeEnum.FULL_STACK,
            ServiceTypeEnum.FRONTEND,
        ]
    ]
    # Get current domain mappings
    domains_map = {d.service_name_slug: d for d in selected_env.domains} if selected_env else {}

    # Find services without domains
    services_needing_domains = [
        s for s in services_with_domains_types if s.name_slug not in domains_map
    ]

    domains = []
    domain_email = selected_env.domain_email if selected_env else None

    if services_needing_domains:
        print("\n[bold]Please provide domain information for your services:[/bold]")

        # Collect email
        if selected_env and not selected_env.domain_email:
            domain_email_questions = [
                inquirer.Text(
                    "domain_email",
                    message="Enter email for SSL (e.g., for Let's Encrypt)",
                    validate=lambda _, x: "@" in x,
                ),
            ]
            domain_email_answers = inquirer.prompt(domain_email_questions)
            domain_email = domain_email_answers["domain_email"]

        # Collect domains
        for service in services_needing_domains:
            domain_questions = [
                inquirer.Text(
                    "domain_name",
                    message=f"Enter domain name for service '{service.name_slug}'",
                    validate=lambda _, x: len(x.strip()) > 0,
                ),
            ]
            domain_answers = inquirer.prompt(domain_questions)
            domains.append(
                DomainInfo(
                    service_name_slug=service.name_slug,
                    domain_name=domain_answers["domain_name"],
                )
            )

    return domain_email, domains


@app.command()
def deploy(ctx: typer.Context):
    """Deploy the application to a specified environment."""
    deployment_config = DeploymentConfig.load(ctx.obj["deployments_path"])
    if not deployment_config:
        print(
            "[bold red]No deployment configuration found. Please run 'opsmith setup' first.[/bold"
            " red]"
        )
        raise typer.Exit(code=1)

    choices = deployment_config.environment_names + ["<Create a new environment>"]

    questions = [
        inquirer.List(
            "environment",
            message=(
                "Select a deployment environment or create a new one (Ex: dev, stage, prod, ...)"
            ),
            choices=choices,
        )
    ]

    answers = inquirer.prompt(questions)
    if not answers:
        raise typer.Exit()

    selected_env_name = answers["environment"]

    if selected_env_name == "<Create a new environment>":
        provider_questions = [
            inquirer.List(
                "cloud_provider",
                message="Select the cloud provider for deployment",
                choices=CLOUD_PROVIDER_REGISTRY.choices,
            ),
        ]
        provider_answers = inquirer.prompt(provider_questions)
        if not provider_answers:
            print("[bold red]Cloud provider selection is required. Aborting.[/bold red]")
            raise typer.Exit(code=1)

        selected_provider_value = provider_answers["cloud_provider"]

        # Initialize the provider
        print(f"Initializing {selected_provider_value} provider...\n")
        provider_class = CLOUD_PROVIDER_REGISTRY.get_provider_class(selected_provider_value)
        try:
            cloud_details = provider_class.get_account_details().model_dump(mode="json")
        except CloudCredentialsError as e:
            print(f"[bold red]Cloud provider authentication/configuration error:\n{e}[/bold red]")
            raise typer.Exit(code=1)
        except Exception as e:
            print(
                "[bold red]An unexpected error occurred while initializing cloud provider or"
                f" fetching details: {e}. Aborting.[/bold red]"
            )
            raise typer.Exit(code=1)

        new_env_questions = [
            inquirer.Text(
                "env_name",
                message="Enter the new environment name",
                validate=lambda _, x: x.strip() != ""
                and x.strip() not in deployment_config.environment_names
                and x.strip() != "<Create a new environment>",
            ),
            inquirer.List(
                "strategy",
                message="Select a deployment strategy for the new environment",
                choices=DEPLOYMENT_STRATEGY_REGISTRY.choices,
            ),
        ]
        new_env_answers = inquirer.prompt(new_env_questions)
        if (
            not new_env_answers
            or not new_env_answers.get("env_name")
            or not new_env_answers.get("strategy")
        ):
            print("[bold red]Environment name and strategy are required. Aborting.[/bold red]")
            raise typer.Exit()

        selected_env_name = new_env_answers["env_name"].strip()
        selected_strategy = new_env_answers["strategy"]

        # Collect domain information using helper method
        domain_email, domains = _collect_domain_configuration(deployment_config)

        new_env = DeploymentEnvironment(
            name=selected_env_name,
            cloud_provider=cloud_details,
            strategy=selected_strategy,
            domains=domains,
            domain_email=domain_email,
        )
        deployment_config.environments.append(new_env)

        deployment_strategy = DEPLOYMENT_STRATEGY_REGISTRY.get_strategy_class(selected_strategy)(
            ctx.obj["agent"],
            ctx.obj["src_dir"],
        )
        deployment_strategy.deploy(deployment_config, new_env)

        deployment_config.save(ctx.obj["deployments_path"])
        print(
            f"\n[bold green]New environment '{selected_env_name}' in region"
            f" '{cloud_details['region']}' with strategy '{selected_strategy}' created and"
            " saved.[/bold green]"
        )
        return

    selected_env = deployment_config.get_environment(selected_env_name)

    action_questions = [
        inquirer.List(
            "action",
            message=f"What would you like to do with the '{selected_env_name}' environment?",
            choices=["release", "update", "run", "delete", "exit"],
            default="release",
        )
    ]
    action_answers = inquirer.prompt(action_questions)
    if not action_answers or action_answers.get("action") == "exit":
        print("Exiting deploy.")
        raise typer.Exit()

    selected_action = action_answers["action"]

    deployment_strategy = DEPLOYMENT_STRATEGY_REGISTRY.get_strategy_class(selected_env.strategy)(
        ctx.obj["agent"],
        ctx.obj["src_dir"],
    )

    if selected_action == "release":
        deployment_strategy.release(deployment_config, selected_env)
        print(f"\nDeployment to '{selected_env_name}' environment completed.")
    elif selected_action == "update":
        # Collect domains using the helper
        new_domain_email, new_domains = _collect_domain_configuration(
            deployment_config, selected_env
        )

        # Update environment
        selected_env.domains.extend(new_domains)
        selected_env.domain_email = new_domain_email

        # Save updated config
        deployment_config.save(ctx.obj["deployments_path"])
        print("[bold green]Domain configuration updated.[/bold green]")

        # Now call update with complete domain information
        deployment_strategy.update(deployment_config, selected_env)
        print(f"\nConfiguration update for '{selected_env_name}' environment completed.")
    elif selected_action == "run":
        runnable_services = [
            s for s in deployment_config.services if s.service_type != ServiceTypeEnum.FRONTEND
        ]
        if not runnable_services:
            print("[bold red]No runnable services found in this project.[/bold red]")
            raise typer.Exit()

        service_choices = [s.name_slug for s in runnable_services]
        service_questions = [
            inquirer.List(
                "service",
                message="Select a service to run a command on",
                choices=service_choices,
            )
        ]
        service_answers = inquirer.prompt(service_questions)
        selected_service_slug = service_answers["service"]

        command_questions = [
            inquirer.Text(
                "command",
                message=f"Enter the command to run on '{selected_service_slug}'",
                validate=lambda _, x: len(x.strip()) > 0,
            )
        ]
        command_answers = inquirer.prompt(command_questions)
        command_to_run = command_answers["command"]

        deployment_strategy.run(
            deployment_config, selected_env, selected_service_slug, command_to_run
        )
        print(f"\nCommand execution on '{selected_env_name}' environment completed.")
    elif selected_action == "delete":
        delete_confirmation_q = [
            inquirer.Text(
                "confirm",
                message=(
                    f"This will delete all infrastructure in the '{selected_env_name}'"
                    " environment. This action cannot be undone. Please type 'DELETE' to confirm."
                ),
            )
        ]
        delete_confirmation_a = inquirer.prompt(delete_confirmation_q)
        if not delete_confirmation_a or delete_confirmation_a.get("confirm") != "DELETE":
            print("[bold yellow]Delete operation cancelled.[/bold yellow]")
            raise typer.Exit()

        deployment_strategy.destroy(deployment_config, selected_env)


@app.command()
def repomap(ctx: typer.Context):
    """
    Generates a map of the repository, showing important files and code elements.
    """
    print("Generating repo map now...")

    repo_mapper = RepoMap(
        src_dir=ctx.obj["src_dir"],
        verbose=ctx.parent.params["verbose"],
    )
    repo_map_str = repo_mapper.get_repo_map()

    if repo_map_str:
        typer.echo(repo_map_str)
    else:
        typer.echo("No git-tracked files found in this repository or failed to generate map.")
