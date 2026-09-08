"""The `deploy` command: the interactive environment menu."""

from typing import List, Optional, Tuple

import inquirer
import typer
from rich import print

from opsmith.cli.commands import requires
from opsmith.cli.state import CliState
from opsmith.cloud_providers import CLOUD_PROVIDER_REGISTRY
from opsmith.deployment_strategies import DEPLOYMENT_STRATEGY_REGISTRY
from opsmith.types import (
    DeploymentConfig,
    DeploymentEnvironment,
    DomainInfo,
    ServiceTypeEnum,
)


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


@requires("docker", "terraform")
def deploy(ctx: typer.Context):
    """Deploy the application to a specified environment."""
    state: CliState = ctx.obj
    deployment_config = DeploymentConfig.load(state.deployments_path)
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
        cloud_details = provider_class.get_account_details(state.context).model_dump(mode="json")

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
            state.context
        )
        deployment_strategy.deploy(deployment_config, new_env)

        deployment_config.save(state.deployments_path)
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
        state.context
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
        deployment_config.save(state.deployments_path)
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
