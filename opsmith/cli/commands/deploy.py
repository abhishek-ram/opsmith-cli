"""The `deploy` command: the interactive environment menu."""

from typing import List, Optional, Tuple

import typer
from rich import print

from opsmith.cli.commands import requires
from opsmith.cli.state import CliState
from opsmith.cloud_providers import CLOUD_PROVIDER_REGISTRY
from opsmith.core.answers import DELETE_CONFIRMATION
from opsmith.core.interaction import Choice, Interaction
from opsmith.deployment_strategies import DEPLOYMENT_STRATEGY_REGISTRY
from opsmith.types import (
    DeploymentConfig,
    DeploymentEnvironment,
    DomainInfo,
    ServiceTypeEnum,
)

#: The option offered alongside the existing environments, and the name no environment may take.
CREATE_NEW_ENVIRONMENT = "<Create a new environment>"


def _must_be_an_email(value: str) -> Optional[str]:
    """
    :param value: What the user typed.
    :return: The problem with it, or None when it will do as an email address.
    """
    if "@" not in value:
        return "Enter an email address."
    return None


def _must_not_be_blank(value: str) -> Optional[str]:
    """
    :param value: What the user typed.
    :return: The problem with it, or None when it holds something.
    """
    if not value.strip():
        return "This cannot be empty."
    return None


def _collect_domain_configuration(
    interact: Interaction,
    deployment_config: DeploymentConfig,
    selected_env: Optional[DeploymentEnvironment] = None,
) -> Tuple[Optional[str], List[DomainInfo]]:
    """
    Collects domain information for services that require domains.

    :param interact: How the run reaches the user.
    :param deployment_config: The configuration whose services need domains.
    :param selected_env: The environment being updated, when one already exists.
    :return: The email to issue certificates with, and one domain per service that needed one.
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
            domain_email = interact.ask(
                "env.domain_email",
                "Enter email for SSL (e.g., for Let's Encrypt)",
                validate=_must_be_an_email,
            )

        # Collect domains
        for service in services_needing_domains:
            domain_name = interact.ask(
                f"env.domain.{service.name_slug}",
                f"Enter domain name for service '{service.name_slug}'",
                validate=_must_not_be_blank,
            )
            domains.append(DomainInfo(service_name_slug=service.name_slug, domain_name=domain_name))

    return domain_email, domains


def _remember_answers_for(state: CliState, environment_name: str):
    """
    Points the answer store and the step ledger at the environment this run is about.

    It happens as soon as the environment is named, and not before, because the question that
    names it is itself an answer: everything asked up to this point is held in memory and written
    out here, into the environment it turned out to belong to.

    :param state: The run's state, holding the context these live on.
    :param environment_name: The environment that was selected or named.
    """
    state.context.answers.use_environment(environment_name)
    state.context.steps.use_environment(environment_name)


@requires("docker", "terraform")
def deploy(ctx: typer.Context):
    """Deploy the application to a specified environment."""
    state: CliState = ctx.obj
    interact = state.context.interact
    deployment_config = DeploymentConfig.load(state.deployments_path)
    if not deployment_config:
        print(
            "[bold red]No deployment configuration found. Please run 'opsmith setup' first.[/bold"
            " red]"
        )
        raise typer.Exit(code=1)

    choices = [Choice(label=name, value=name) for name in deployment_config.environment_names] + [
        Choice(label=CREATE_NEW_ENVIRONMENT, value=CREATE_NEW_ENVIRONMENT)
    ]

    selected_env_name = interact.select(
        "env.name",
        "Select a deployment environment or create a new one (Ex: dev, stage, prod, ...)",
        choices,
    )

    if selected_env_name != CREATE_NEW_ENVIRONMENT:
        _remember_answers_for(state, selected_env_name)

    if selected_env_name == CREATE_NEW_ENVIRONMENT:
        selected_provider_value = interact.select(
            "env.cloud_provider",
            "Select the cloud provider for deployment",
            CLOUD_PROVIDER_REGISTRY.choices,
        )

        # Initialize the provider
        print(f"Initializing {selected_provider_value} provider...\n")
        provider_class = CLOUD_PROVIDER_REGISTRY.get_provider_class(selected_provider_value)
        cloud_details = provider_class.get_account_details(state.context).model_dump(mode="json")

        def _is_a_free_environment_name(value: str) -> Optional[str]:
            """
            :param value: The name the user typed.
            :return: The problem with it, or None when it can name a new environment.
            """
            name = value.strip()
            if not name or name == CREATE_NEW_ENVIRONMENT:
                return "Enter a name for the new environment."
            if name in deployment_config.environment_names:
                return f"An environment named '{name}' already exists."
            return None

        selected_env_name = interact.ask(
            "env.name",
            "Enter the new environment name",
            validate=_is_a_free_environment_name,
        ).strip()
        _remember_answers_for(state, selected_env_name)

        selected_strategy = interact.select(
            "env.strategy",
            "Select a deployment strategy for the new environment",
            DEPLOYMENT_STRATEGY_REGISTRY.choices,
        )

        # Collect domain information using helper method
        domain_email, domains = _collect_domain_configuration(interact, deployment_config)

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

    selected_action = interact.select(
        "env.action",
        f"What would you like to do with the '{selected_env_name}' environment?",
        [
            Choice(label=action, value=action)
            for action in ["release", "update", "run", "delete", "exit"]
        ],
        default="release",
    )
    if selected_action == "exit":
        print("Exiting deploy.")
        raise typer.Exit()

    deployment_strategy = DEPLOYMENT_STRATEGY_REGISTRY.get_strategy_class(selected_env.strategy)(
        state.context
    )

    if selected_action == "release":
        deployment_strategy.release(deployment_config, selected_env)
        print(f"\nDeployment to '{selected_env_name}' environment completed.")
    elif selected_action == "update":
        # Collect domains using the helper
        new_domain_email, new_domains = _collect_domain_configuration(
            interact, deployment_config, selected_env
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

        service_choices = [Choice(label=s.name_slug, value=s.name_slug) for s in runnable_services]
        selected_service_slug = interact.select(
            "run.service", "Select a service to run a command on", service_choices
        )

        command_to_run = interact.ask(
            "run.command",
            f"Enter the command to run on '{selected_service_slug}'",
            validate=_must_not_be_blank,
        )

        deployment_strategy.run(
            deployment_config, selected_env, selected_service_slug, command_to_run
        )
        print(f"\nCommand execution on '{selected_env_name}' environment completed.")
    elif selected_action == "delete":
        typed = interact.ask(
            "delete.confirm",
            (
                f"This will delete all infrastructure in the '{selected_env_name}'"
                " environment. This action cannot be undone. Please type"
                f" '{DELETE_CONFIRMATION}' to confirm."
            ),
        )
        if typed != DELETE_CONFIRMATION:
            print("[bold yellow]Delete operation cancelled.[/bold yellow]")
            raise typer.Exit()

        deployment_strategy.destroy(deployment_config, selected_env)
