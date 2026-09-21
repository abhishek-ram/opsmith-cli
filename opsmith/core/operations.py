"""Everything Opsmith can do to a repository, as functions rather than as a menu.

Until this module existed, every operation lived inside the body of the interactive ``deploy``
command: selecting an environment, detecting a cloud account, collecting domains, and dispatching
to a strategy. Nothing could call any of it except a person at a terminal.

Each of those is a function here now, taking the run's context and returning a typed result. The
interactive menu and the headless subcommands both call these and nothing else, which is the rule
that keeps the two from drifting: **the menu never reaches a strategy or a provisioner**. It
resolves a choice to one of these functions and calls it, exactly as the subcommand does.

Nothing here knows about a terminal. Questions go through ``ctx.interact``, progress through
``ctx.events``, and what a run stopped for is the caller's problem, not this module's.
"""

import inspect
from dataclasses import replace
from pathlib import Path
from typing import Any, List, Optional, Tuple, Type, TypeVar

import yaml

from opsmith.cloud_providers import CLOUD_PROVIDER_REGISTRY
from opsmith.cloud_providers.base import BaseCloudProvider, BaseCloudProviderDetail
from opsmith.core.answers import DELETE_CONFIRMATION, AnswerSources
from opsmith.core.config import ConfigIssue, parse_infra_deps, parse_service
from opsmith.core.context import OpsmithContext
from opsmith.core.errors import (
    InvalidArgument,
    InvalidConfig,
    OpsmithError,
    UnknownService,
)
from opsmith.core.events import STEP_CONFIG, STEP_DETECT, STEP_DNS, STEP_SETUP
from opsmith.core.interaction import Choice
from opsmith.core.questions import (
    Question,
    Resolution,
    Variant,
    ask_all,
    evaluate,
    skeleton,
)
from opsmith.core.results import (
    DestroyResult,
    EnvCreateResult,
    EnvironmentSummary,
    EnvListResult,
    EnvPlanResult,
    EnvStatusResult,
    InitResult,
    OperationResult,
    ReleaseResult,
    RunResult,
    SetupResult,
    UpdateResult,
)
from opsmith.deployment_strategies import DEPLOYMENT_STRATEGY_REGISTRY
from opsmith.deployment_strategies.base import BaseDeploymentStrategy
from opsmith.service_detector import ServiceDetector
from opsmith.settings import settings
from opsmith.types import (
    DeploymentConfig,
    DeploymentEnvironment,
    DomainInfo,
    ServiceInfo,
    ServiceTypeEnum,
)
from opsmith.utils import slugify

#: The option offered alongside the existing environments, and the name no environment may take.
CREATE_NEW_ENVIRONMENT = "<Create a new environment>"

#: The service types that answer requests at a domain, and so need one configured.
ROUTED_SERVICE_TYPES = (
    ServiceTypeEnum.BACKEND_API,
    ServiceTypeEnum.FULL_STACK,
    ServiceTypeEnum.FRONTEND,
)


# --- the small rules a question applies to what it is told ----------------------------------


def must_be_an_email(value: str) -> Optional[str]:
    """
    :param value: What the user typed.
    :return: The problem with it, or None when it will do as an email address.
    """
    if "@" not in value:
        return "Enter an email address."
    return None


def must_not_be_blank(value: str) -> Optional[str]:
    """
    :param value: What the user typed.
    :return: The problem with it, or None when it holds something.
    """
    if not value.strip():
        return "This cannot be empty."
    return None


# --- where things are, and what a run remembers ---------------------------------------------


def state_path(ctx: OpsmithContext, environment_name: str) -> Path:
    """
    Returns the state file of an environment.

    That file existing is what says an environment has been deployed, which is the one fact about
    a deployment that is the same under every strategy - the contents are the strategy's own, and
    only it reads them.

    :param ctx: The run's context.
    :param environment_name: The environment being asked about.
    :return: The path the strategy writes its state to.
    """
    return ctx.deployments_path / "environments" / environment_name / "state.yml"


def is_deployed(ctx: OpsmithContext, environment_name: str) -> bool:
    """
    :param ctx: The run's context.
    :param environment_name: The environment being asked about.
    :return: Whether it has been deployed.
    """
    return state_path(ctx, environment_name).exists()


def bind_environment(ctx: OpsmithContext, environment_name: str):
    """
    Points the answer store and the step ledger at the environment this run is about.

    It happens as soon as the environment is named, and not before, because the question that
    names it is itself an answer: everything asked up to this point is held in memory and written
    out here, into the environment it turned out to belong to.

    :param ctx: The run's context, holding both.
    :param environment_name: The environment that was selected or named.
    """
    ctx.answers.use_environment(environment_name)
    ctx.steps.use_environment(environment_name)


def load_config(ctx: OpsmithContext) -> DeploymentConfig:
    """
    Reads the deployment configuration, insisting that there is one.

    :param ctx: The run's context.
    :return: What the repository deploys.
    :raises InvalidConfig: The repository has not been set up.
    """
    deployment_config = DeploymentConfig.load(ctx.deployments_path)
    if deployment_config is None:
        raise InvalidConfig(
            "No deployment configuration found.",
            hint="Run 'opsmith setup' to detect what this repository deploys.",
            details={"path": str(ctx.deployments_path)},
        )
    return deployment_config


def strategy_for(ctx: OpsmithContext, environment: DeploymentEnvironment) -> BaseDeploymentStrategy:
    """
    Builds the strategy an environment was created with.

    :param ctx: The run's context, which is all a strategy takes.
    :param environment: The environment whose strategy is wanted.
    :return: The strategy, ready to be asked to do something.
    """
    return DEPLOYMENT_STRATEGY_REGISTRY.get_strategy_class(environment.strategy)(ctx)


ResultT = TypeVar("ResultT", bound=OperationResult)


def reported(ctx: OpsmithContext, result: ResultT) -> ResultT:
    """
    Attaches what the run told the user to the result it is about to return.

    A terminal user read the notices as they went past; a driver reads them here. Both
    implementations of the interaction collect them, so this does not care which one ran.

    The result type is carried through, so a caller returning ``reported(ctx, ReleaseResult(...))``
    still returns a ``ReleaseResult`` rather than the base class.

    :param ctx: The run's context.
    :param result: The result being returned.
    :return: The same result, carrying the notices and next steps.
    """
    result.notices = list(ctx.interact.notices)
    result.next_steps = list(ctx.interact.next_steps)
    return result


# --- setup ------------------------------------------------------------------------------------


def _report_issues(ctx: OpsmithContext, heading: str, issues: List[ConfigIssue]):
    """
    Shows the problems found in an edited document, so the editor can be reopened on them.

    :param ctx: The run's context.
    :param heading: What was being edited.
    :param issues: The problems found in it.
    """
    for issue in issues:
        location = f"{issue.path}: " if issue.path else ""
        ctx.interact.notify(f"{heading}: {location}{issue.message}", details=issue.model_dump())


def _review(ctx: OpsmithContext, key: str, message: str, heading: str, document: str, parse):
    """
    Hands a proposed document to the user and reopens the editor until it parses.

    The reopening used to be inquirer's, driven by a validator that printed as a side effect.
    It is a plain loop now, because the same rules have to hold for a run with nobody at the
    keyboard, and only the caller knows what to do with what comes back.

    The loop terminates with nobody there too: a headless review editor accepts a proposal once,
    and refuses the same key a second time, because being asked again means what it accepted did
    not parse and accepting it again would only not parse again.

    :param ctx: The run's context.
    :param key: The interaction key the edit is addressed by.
    :param message: What the user is being asked to review.
    :param heading: How to label a problem found in what they saved.
    :param document: The proposal, as YAML.
    :param parse: Turns the edited YAML into the object, or into the issues that stopped it.
    :return: Whatever ``parse`` produced once it produced something.
    """
    while True:
        document = ctx.interact.edit(key, message, content=document, on_headless="accept")
        parsed, issues = parse(document)
        if parsed is not None:
            return parsed
        _report_issues(ctx, heading, issues)


def init_config(ctx: OpsmithContext) -> InitResult:
    """
    Writes a deployment configuration for a repository, without scanning it.

    This is the half of ``setup`` that costs nothing: naming the application and creating the
    file. It exists on its own so a harness can author the services itself and have Opsmith
    validate them, rather than having to sit through a detection run it intends to discard.

    :param ctx: The run's context.
    :return: What was written.
    :raises InvalidConfig: The repository already has a configuration.
    """
    if DeploymentConfig.load(ctx.deployments_path) is not None:
        raise InvalidConfig(
            "This repository already has a deployment configuration.",
            hint="Run 'opsmith setup' to re-scan it, or edit .opsmith/deployments.yml directly.",
            details={"path": str(ctx.deployments_path)},
        )

    app_name = ctx.interact.ask(
        "app.name", "Enter the application name", validate=must_not_be_blank
    )
    deployment_config = DeploymentConfig(app_name=app_name, app_name_slug=slugify(app_name))
    ctx.git_repo.ensure_gitignore()

    config_path = deployment_config.save(ctx.deployments_path)
    ctx.events.log(STEP_CONFIG, f"Deployment configuration written to: {config_path}")

    return reported(
        ctx,
        InitResult(
            app_name=deployment_config.app_name,
            app_name_slug=deployment_config.app_name_slug,
            config_path=str(config_path),
        ),
    )


def run_setup(ctx: OpsmithContext) -> SetupResult:
    """
    Detects what the repository deploys, confirms it, and writes the Dockerfiles.

    :param ctx: The run's context.
    :return: What was detected and written.
    """
    detector = ServiceDetector(ctx=ctx)
    deployment_config = DeploymentConfig.load(ctx.deployments_path)

    if deployment_config:
        ctx.events.step(STEP_SETUP, "Existing deployment configuration found.")
        action = ctx.interact.select(
            "setup.action",
            "What would you like to do?",
            [Choice(label="Re-scan services", value="rescan"), Choice(label="Exit", value="exit")],
            default="exit",
        )
        if action == "exit":
            # Nothing is written. Saving an unchanged configuration would rewrite the file the
            # user just declined to change, and would touch its timestamp for no reason.
            ctx.events.log(STEP_SETUP, "Exiting setup.")
            return reported(ctx, _setup_result(ctx, deployment_config, dockerfiles=[]))
    else:
        ctx.events.step(STEP_SETUP, "No existing deployment configuration found.")
        app_name = ctx.interact.ask(
            "app.name", "Enter the application name", validate=must_not_be_blank
        )
        deployment_config = DeploymentConfig(app_name=app_name, app_name_slug=slugify(app_name))
        ctx.git_repo.ensure_gitignore()

    ctx.events.step(
        STEP_DETECT,
        "Scanning your codebase now to detect services, frameworks, and languages...",
    )
    service_list_obj = detector.detect_services(existing_config=deployment_config)

    dockerfiles: List[str] = []
    confirmed_services = []
    for index, service in enumerate(service_list_obj.services):
        service_yaml = yaml.dump(service.model_dump(mode="json"), indent=2)
        confirmed_service = _review(
            ctx,
            f"service.{service.name_slug}.confirm",
            f"Review and confirm Service {index + 1}/{len(service_list_obj.services)}",
            "Invalid service configuration",
            service_yaml,
            parse_service,
        )
        confirmed_services.append(confirmed_service)

        dockerfile_path = detector.generate_dockerfile(service=confirmed_service)
        if dockerfile_path is not None:
            dockerfiles.append(str(dockerfile_path))

    deployment_config.services = confirmed_services

    infra_deps = service_list_obj.infra_deps
    if infra_deps:
        deps_yaml = yaml.dump([dep.model_dump(mode="json") for dep in infra_deps], indent=2)
        deployment_config.infra_deps = _review(
            ctx,
            "infra_deps.confirm",
            (
                "Review and confirm dependencies.\nIf 'provider' is 'user_choice', please"
                " replace it with a valid provider.\nEach provider can only be listed once."
            ),
            "Invalid dependency configuration",
            deps_yaml,
            parse_infra_deps,
        )

    config_path = deployment_config.save(ctx.deployments_path)
    ctx.events.log(STEP_CONFIG, f"Deployment configuration saved to: {config_path}")

    return reported(ctx, _setup_result(ctx, deployment_config, dockerfiles=dockerfiles))


def _setup_result(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, *, dockerfiles: List[str]
) -> SetupResult:
    """
    Describes what the configuration now holds.

    :param ctx: The run's context, for the path the configuration lives at.
    :param deployment_config: The configuration as it now stands.
    :param dockerfiles: The Dockerfiles this run wrote, which is none when it wrote none.
    :return: The result.
    """
    return SetupResult(
        app_name=deployment_config.app_name,
        services=deployment_config.services,
        dockerfiles=dockerfiles,
        infra_deps=deployment_config.infra_deps,
        config_path=str(ctx.deployments_path / settings.config_filename),
    )


# --- environments -------------------------------------------------------------------------------


def list_environments(ctx: OpsmithContext, deployment_config: DeploymentConfig) -> EnvListResult:
    """
    Reports the environments the configuration declares, and which of them exist.

    Nothing here reaches a cloud, so it answers on a machine with no tooling installed at all.

    :param ctx: The run's context.
    :param deployment_config: What the repository deploys.
    :return: One summary per environment.
    """
    return reported(
        ctx,
        EnvListResult(
            environments=[
                EnvironmentSummary(
                    name=environment.name,
                    provider=str(environment.cloud_provider.get("name", "")),
                    region=environment.cloud_provider_detail.region,
                    strategy=environment.strategy,
                    deployed=is_deployed(ctx, environment.name),
                )
                for environment in deployment_config.environments
            ]
        ),
    )


def environment_status(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, environment_name: str
) -> EnvStatusResult:
    """
    Reports what an environment is running, from its state file alone.

    :param ctx: The run's context.
    :param deployment_config: What the repository deploys.
    :param environment_name: The environment being asked about.
    :return: What the last deploy or update left behind.
    :raises UnknownEnvironment: The configuration declares no such environment.
    """
    environment = deployment_config.get_environment(environment_name)
    strategy = strategy_for(ctx, environment)
    return reported(ctx, strategy.status(deployment_config, environment))


def select_environment(ctx: OpsmithContext, deployment_config: DeploymentConfig) -> str:
    """
    Asks which environment a run is about, offering the option to name a new one.

    :param ctx: The run's context.
    :param deployment_config: What the repository deploys.
    :return: The environment's name, or :data:`CREATE_NEW_ENVIRONMENT`.
    """
    choices = [Choice(label=name, value=name) for name in deployment_config.environment_names]
    choices.append(Choice(label=CREATE_NEW_ENVIRONMENT, value=CREATE_NEW_ENVIRONMENT))

    return ctx.interact.select(
        "env.name",
        "Select a deployment environment or create a new one (Ex: dev, stage, prod, ...)",
        choices,
    )


def services_needing_domains(
    deployment_config: DeploymentConfig,
    environment: Optional[DeploymentEnvironment] = None,
) -> List[ServiceInfo]:
    """
    Finds the services that answer requests at a domain and do not have one yet.

    :param deployment_config: What the repository deploys.
    :param environment: The environment being created or updated, when one already exists.
    :return: The services still to be given a domain, in configuration order.
    """
    configured = (
        {domain.service_name_slug for domain in environment.domains} if environment else set()
    )
    return [
        service
        for service in deployment_config.services
        if service.service_type in ROUTED_SERVICE_TYPES and service.name_slug not in configured
    ]


def domain_questions(
    deployment_config: DeploymentConfig,
    environment: Optional[DeploymentEnvironment] = None,
) -> List[Question]:
    """
    Declares the domain questions an environment asks, which is also what asks them.

    There are none at all when every routed service already has a domain, and no email question
    when the environment already recorded one - the same two conditions the flow has always
    applied, expressed once so that ``env plan`` and ``env create`` cannot disagree about them.

    :param deployment_config: What the repository deploys.
    :param environment: The environment being created or updated, when one already exists.
    :return: The email question and one question per service still needing a domain.
    """
    services = services_needing_domains(deployment_config, environment)
    if not services:
        return []

    questions: List[Question] = []
    if not (environment and environment.domain_email):
        questions.append(
            Question(
                key="env.domain_email",
                message="Enter email for SSL (e.g., for Let's Encrypt)",
                asked_by="env create",
                validate=must_be_an_email,
            )
        )

    questions.append(
        Question(
            key="env.domain.<slug>",
            message="Enter the domain name of a service",
            asked_by="env create",
            validate=must_not_be_blank,
            for_each=lambda _: [
                Variant(
                    token=service.name_slug,
                    message=f"Enter domain name for service '{service.name_slug}'",
                )
                for service in services
            ],
        )
    )
    return questions


def environment_questions(
    deployment_config: DeploymentConfig,
    environment: Optional[DeploymentEnvironment] = None,
) -> List[Question]:
    """
    Declares everything making an environment asks, whichever provider and strategy it uses.

    These are asked inline by :func:`create_environment`, which has a validator and a default to
    apply that a declaration cannot carry. Declaring them as well is what lets ``env plan``
    report them, and a test checks that each key here is one the flow actually asks.

    :param deployment_config: What the repository deploys.
    :param environment: The environment being created, when it is already in the configuration.
    :return: The provider, name, strategy and domain questions, in the order they are asked.
    """
    return [
        Question(
            key="env.cloud_provider",
            message="Select the cloud provider for deployment",
            primitive="select",
            asked_by="env create",
            choices=lambda _: CLOUD_PROVIDER_REGISTRY.choices,
        ),
        Question(
            key="env.name",
            message="Enter the new environment name",
            asked_by="env create",
            validate=must_not_be_blank,
        ),
        Question(
            key="env.strategy",
            message="Select a deployment strategy for the new environment",
            primitive="select",
            asked_by="env create",
            default=environment.strategy if environment else None,
            choices=lambda _: DEPLOYMENT_STRATEGY_REGISTRY.choices,
        ),
    ] + domain_questions(deployment_config, environment)


def collect_domain_configuration(
    ctx: OpsmithContext,
    deployment_config: DeploymentConfig,
    environment: Optional[DeploymentEnvironment] = None,
) -> Tuple[Optional[str], List[DomainInfo]]:
    """
    Collects domain information for services that require domains.

    The questions come from :func:`domain_questions` rather than from a loop here, so that what
    ``env plan`` promises and what this asks are the same list read twice.

    :param ctx: The run's context.
    :param deployment_config: The configuration whose services need domains.
    :param environment: The environment being updated, when one already exists.
    :return: The email to issue certificates with, and one domain per service that needed one.
    """
    questions = domain_questions(deployment_config, environment)
    domain_email = environment.domain_email if environment else None
    if not questions:
        return domain_email, []

    ctx.events.step(STEP_DNS, "Please provide domain information for your services:")

    answers = ask_all(
        ctx.interact,
        questions,
        Resolution(events=ctx.events, deployment_config=deployment_config),
    )

    domains = [
        DomainInfo(service_name_slug=key[len("env.domain.") :], domain_name=value)
        for key, value in answers.items()
        if key.startswith("env.domain.")
    ]
    return answers.get("env.domain_email", domain_email), domains


def account_detail(
    ctx: OpsmithContext, provider_class: Type[BaseCloudProvider]
) -> BaseCloudProviderDetail:
    """
    Reaches the cloud account and asks whatever the provider needs choosing about it.

    Three steps, because they are three different things: detecting an account asks nobody
    anything and is what ``env plan`` can also do, the declared questions are asked through the
    run's interaction like any other, and building the detail is the provider turning both into
    the record the environment keeps.

    :param ctx: The run's context.
    :param provider_class: The provider that was chosen.
    :return: What the environment records about the provider.
    :raises CloudCredentialsError: The account could not be reached.
    """
    account = provider_class.detect_account(ctx)
    answers = ask_all(
        ctx.interact,
        provider_class.questions(),
        Resolution(events=ctx.events, account=account),
    )
    return provider_class.build_detail(ctx, account, answers)


def create_environment(
    ctx: OpsmithContext,
    deployment_config: DeploymentConfig,
    *,
    deploy: bool = True,
) -> EnvCreateResult:
    """
    Creates an environment and, unless told not to, deploys it.

    The questions are asked in the order the interactive menu has always asked them - provider
    first, because detecting the account is what makes the region list real, then the name, then
    the strategy and the domains. A flag answers any of them, so the order only matters to a
    person watching.

    An environment that is already in the configuration but has never been deployed is a creation
    that stopped part way, so this picks it up rather than refusing the name: every answer it
    already gave is in the store, and every step it already finished is in the ledger.

    :param ctx: The run's context.
    :param deployment_config: What the repository deploys.
    :param deploy: Whether to deploy the environment, or only write it to the configuration.
    :return: What was created.
    """
    selected_provider = ctx.interact.select(
        "env.cloud_provider",
        "Select the cloud provider for deployment",
        CLOUD_PROVIDER_REGISTRY.choices,
    )

    ctx.events.step(STEP_SETUP, f"Initializing {selected_provider} provider...")
    provider_class = CLOUD_PROVIDER_REGISTRY.get_provider_class(selected_provider)
    cloud_details = account_detail(ctx, provider_class).model_dump(mode="json")

    def is_a_usable_environment_name(value: str) -> Optional[str]:
        """
        :param value: The name the user typed.
        :return: The problem with it, or None when it can name this environment.
        """
        name = value.strip()
        if not name or name == CREATE_NEW_ENVIRONMENT:
            return "Enter a name for the new environment."
        if is_deployed(ctx, name):
            return f"An environment named '{name}' already exists."
        return None

    environment_name = ctx.interact.ask(
        "env.name", "Enter the new environment name", validate=is_a_usable_environment_name
    ).strip()
    bind_environment(ctx, environment_name)

    existing = next(
        (env for env in deployment_config.environments if env.name == environment_name), None
    )
    selected_strategy = ctx.interact.select(
        "env.strategy",
        "Select a deployment strategy for the new environment",
        DEPLOYMENT_STRATEGY_REGISTRY.choices,
        default=existing.strategy if existing else None,
    )

    domain_email, domains = collect_domain_configuration(ctx, deployment_config, existing)
    if existing is not None:
        existing.cloud_provider = cloud_details
        existing.strategy = selected_strategy
        existing.domains.extend(domains)
        existing.domain_email = domain_email
        environment = existing
    else:
        environment = DeploymentEnvironment(
            name=environment_name,
            cloud_provider=cloud_details,
            strategy=selected_strategy,
            domains=domains,
            domain_email=domain_email,
        )
        deployment_config.environments.append(environment)

    if not deploy:
        # Written now precisely because nothing will be deployed: the configuration is the whole
        # output of this run, and a later `env create` picks the environment up from here.
        config_path = deployment_config.save(ctx.deployments_path)
        ctx.events.log(STEP_CONFIG, f"Deployment configuration saved to: {config_path}")
        return reported(
            ctx,
            EnvCreateResult(
                environment=environment.name,
                provider=str(cloud_details.get("name", "")),
                region=environment.cloud_provider_detail.region,
                strategy=selected_strategy,
                deployed=False,
            ),
        )

    result = strategy_for(ctx, environment).deploy(deployment_config, environment)

    config_path = deployment_config.save(ctx.deployments_path)
    ctx.events.log(STEP_CONFIG, f"Deployment configuration saved to: {config_path}")
    ctx.events.log(
        STEP_SETUP,
        (
            f"New environment '{environment.name}' in region"
            f" '{environment.cloud_provider_detail.region}' with strategy"
            f" '{selected_strategy}' created and saved."
        ),
    )

    return reported(ctx, result)


def declares_questions(implementation: type, base: type) -> bool:
    """
    Reports whether a plugin wrote a question tree of its own.

    Declaring is optional, so a class that did not override ``questions`` is not broken - it asks
    inline and works exactly as before. It does mean a plan of it is partial, and saying so is
    the difference between a short list and a wrong one.

    :param implementation: The provider or strategy class in use.
    :param base: The class whose ``questions`` is the do-nothing default.
    :return: Whether the implementation declares its own questions.
    """
    # getattr_static looks the attribute up through the MRO without running the descriptor
    # protocol, so this compares the two underlying classmethod objects rather than the freshly
    # bound methods that plain attribute access would hand back.
    return inspect.getattr_static(implementation, "questions") is not inspect.getattr_static(
        base, "questions"
    )


def plan_environment(
    ctx: OpsmithContext,
    deployment_config: DeploymentConfig,
    *,
    sources: AnswerSources,
    write_answers: Optional[Path] = None,
) -> EnvPlanResult:
    """
    Reports every answer creating an environment will need, before anything is created.

    Nothing here writes to a cloud or to the repository. It may *read* from a cloud - listing
    the regions of an account makes the report far more useful - but every such read is allowed
    to fail: a plan made on a machine that has no credentials yet is still worth printing, and
    says which parts of it went unlisted.

    The list forks on two answers. Which questions a provider asks is the provider's business,
    and the same for a strategy, so until those two are chosen the rest cannot be enumerated.
    That is the round: answer them, run this again, and the branch they open is reported.

    :param ctx: The run's context.
    :param deployment_config: What the repository deploys.
    :param sources: What this run was told up front, consulted alongside the answer store to
        decide which questions are already answered.
    :param write_answers: Where to write the skeleton answers file, when one was asked for.
    :return: What is needed, what is known, and why the list is or is not complete.
    """

    def known(key: str) -> Tuple[bool, Any]:
        """
        :param key: The interaction key.
        :return: Whether this run already has an answer for it, and what it is.
        """
        found, value, _ = sources.supplied(key)
        if found:
            return True, value
        if ctx.answers.has(key):
            return True, ctx.answers.get(key)
        return False, None

    environment_name = known("env.name")[1] if known("env.name")[0] else None
    if environment_name:
        bind_environment(ctx, str(environment_name))

    existing = next(
        (env for env in deployment_config.environments if env.name == environment_name), None
    )

    resolution = Resolution(events=ctx.events, deployment_config=deployment_config, lookup=known)

    questions = environment_questions(deployment_config, existing)
    reasons: List[str] = []

    provider_name = resolution.get("env.cloud_provider")
    provider_questions, provider_reasons, account = _provider_plan(ctx, provider_name)
    reasons.extend(provider_reasons)

    strategy_name = resolution.get("env.strategy")
    strategy_questions, strategy_reasons = _strategy_plan(strategy_name)
    reasons.extend(strategy_reasons)

    # The provider's questions come straight after the provider is chosen, and the strategy's
    # last, which is the order a creation asks them in. A plan a person reads top to bottom is
    # the plan they are about to live through.
    ordered = questions[:1] + provider_questions + questions[1:] + strategy_questions
    evaluation = evaluate(
        ordered,
        replace(resolution, account=account),
        accept_defaults=sources.accept_defaults,
        tolerant=True,
    )
    reasons.extend(evaluation.notes)

    answers_file = None
    if write_answers is not None:
        write_answers.parent.mkdir(parents=True, exist_ok=True)
        write_answers.write_text(skeleton(evaluation.needed), encoding="utf-8")
        answers_file = str(write_answers)
        ctx.events.log(STEP_CONFIG, f"Answers skeleton written to: {answers_file}")

    return reported(
        ctx,
        EnvPlanResult(
            environment=str(environment_name) if environment_name else None,
            provider=str(provider_name) if provider_name else None,
            strategy=str(strategy_name) if strategy_name else None,
            complete=evaluation.complete and not reasons,
            answers_needed=evaluation.needed,
            answers_known=evaluation.known,
            blocked_on=evaluation.blocked_on,
            partial_reasons=reasons,
            answers_file=answers_file,
        ),
    )


def _provider_plan(
    ctx: OpsmithContext, provider_name: Optional[Any]
) -> Tuple[List[Question], List[str], Optional[Any]]:
    """
    Fetches the chosen provider's questions, and its account when it can be reached.

    :param ctx: The run's context.
    :param provider_name: What ``env.cloud_provider`` is answered with, when it is.
    :return: The provider's questions, why the plan is partial, and the detected account.
    :raises InvalidArgument: The name does not belong to a registered provider.
    """
    if not provider_name:
        return (
            [],
            ["The cloud provider has not been chosen, so its questions are not listed."],
            None,
        )

    provider_class = CLOUD_PROVIDER_REGISTRY.get_provider_class(str(provider_name))
    reasons: List[str] = []
    if not declares_questions(provider_class, BaseCloudProvider):
        reasons.append(
            f"The {provider_class.name()} provider declares no questions, so anything it asks is"
            " not listed here."
        )

    # Detecting the account is what makes a region list real, and it is also the first thing that
    # fails on a machine with no credentials. A plan is worth having either way, so this is the
    # one call here that is allowed to come to nothing.
    account = None
    try:
        account = provider_class.detect_account(ctx)
    except OpsmithError as failure:
        reasons.append(f"Could not reach the {provider_class.name()} account: {failure.message}")

    return provider_class.questions(), reasons, account


def _strategy_plan(strategy_name: Optional[Any]) -> Tuple[List[Question], List[str]]:
    """
    Fetches the chosen strategy's questions.

    :param strategy_name: What ``env.strategy`` is answered with, when it is.
    :return: The strategy's questions, and why the plan is partial.
    :raises InvalidArgument: The name does not belong to a registered strategy.
    """
    if not strategy_name:
        return [], ["The deployment strategy has not been chosen, so its questions are not listed."]

    strategy_class = DEPLOYMENT_STRATEGY_REGISTRY.get_strategy_class(str(strategy_name))
    if not declares_questions(strategy_class, BaseDeploymentStrategy):
        return [], [
            f"The {strategy_class.name()} strategy declares no questions, so anything it asks is"
            " not listed here."
        ]

    return strategy_class.questions(), []


def release(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, environment_name: str
) -> ReleaseResult:
    """
    Builds the current code and deploys it to an environment that already exists.

    :param ctx: The run's context.
    :param deployment_config: What the repository deploys.
    :param environment_name: The environment being released to.
    :return: What was built and released.
    :raises UnknownEnvironment: The configuration declares no such environment.
    """
    environment = deployment_config.get_environment(environment_name)
    bind_environment(ctx, environment_name)

    result = strategy_for(ctx, environment).release(deployment_config, environment)
    ctx.events.log(STEP_SETUP, f"Deployment to '{environment_name}' environment completed.")
    return reported(ctx, result)


def update(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, environment_name: str
) -> UpdateResult:
    """
    Reconciles a deployed environment with the configuration as it now stands.

    Any domain a newly added service needs is collected first and saved, so the update runs
    against a complete configuration rather than one it would have to ask about half way through.

    :param ctx: The run's context.
    :param deployment_config: What the repository deploys.
    :param environment_name: The environment being updated.
    :return: What changed, or why nothing did.
    :raises UnknownEnvironment: The configuration declares no such environment.
    """
    environment = deployment_config.get_environment(environment_name)
    bind_environment(ctx, environment_name)

    domain_email, domains = collect_domain_configuration(ctx, deployment_config, environment)
    environment.domains.extend(domains)
    environment.domain_email = domain_email

    config_path = deployment_config.save(ctx.deployments_path)
    ctx.events.log(STEP_CONFIG, f"Domain configuration updated in: {config_path}")

    result = strategy_for(ctx, environment).update(deployment_config, environment)
    ctx.events.log(
        STEP_SETUP, f"Configuration update for '{environment_name}' environment completed."
    )
    return reported(ctx, result)


def run_command(
    ctx: OpsmithContext,
    deployment_config: DeploymentConfig,
    environment_name: str,
    *,
    service_name_slug: Optional[str] = None,
    command: Optional[str] = None,
) -> RunResult:
    """
    Runs a one-off command on a deployed service.

    The service and the command are arguments rather than answers resolved from the store: they
    describe this invocation and nothing else, which is why the key table calls them positional
    and why the store refuses to remember them.

    :param ctx: The run's context.
    :param deployment_config: What the repository deploys.
    :param environment_name: The environment to run in.
    :param service_name_slug: The service to run on. Asked for when it is not given.
    :param command: The command to run. Asked for when it is not given.
    :return: What the command exited with, and the tail of what it wrote.
    :raises UnknownEnvironment: The configuration declares no such environment.
    :raises UnknownService: The named service is not one that can run a command.
    """
    environment = deployment_config.get_environment(environment_name)
    bind_environment(ctx, environment_name)

    runnable_services = [
        service
        for service in deployment_config.services
        if service.service_type != ServiceTypeEnum.FRONTEND
    ]
    if not runnable_services:
        raise UnknownService(
            "No runnable services found in this project.",
            hint="A command can only be run on a service that runs in a container.",
            details={"environment": environment_name},
        )

    if service_name_slug is None:
        service_name_slug = ctx.interact.select(
            "run.service",
            "Select a service to run a command on",
            [
                Choice(label=service.name_slug, value=service.name_slug)
                for service in runnable_services
            ],
        )
    elif service_name_slug not in [service.name_slug for service in runnable_services]:
        raise UnknownService(
            f"'{service_name_slug}' is not a service a command can be run on.",
            hint="Run 'opsmith config show' to see the services this repository declares.",
            details={
                "service": service_name_slug,
                "runnable": [service.name_slug for service in runnable_services],
            },
        )

    if command is None:
        command = ctx.interact.ask(
            "run.command",
            f"Enter the command to run on '{service_name_slug}'",
            validate=must_not_be_blank,
        )

    result = strategy_for(ctx, environment).run(
        deployment_config, environment, service_name_slug, command
    )
    ctx.events.log(STEP_SETUP, f"Command execution on '{environment_name}' environment completed.")
    return reported(ctx, result)


def destroy_environment(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, environment_name: str
) -> DestroyResult:
    """
    Tears an environment down, after being told to in as many words.

    :param ctx: The run's context.
    :param deployment_config: What the repository deploys.
    :param environment_name: The environment being destroyed.
    :return: What was torn down.
    :raises UnknownEnvironment: The configuration declares no such environment, or the person
        asked to confirm the deletion did not.
    """
    environment = deployment_config.get_environment(environment_name)
    bind_environment(ctx, environment_name)

    typed = ctx.interact.ask(
        "delete.confirm",
        (
            f"This will delete all infrastructure in the '{environment_name}' environment. This"
            f" action cannot be undone. Please type '{DELETE_CONFIRMATION}' to confirm."
        ),
    )
    if typed != DELETE_CONFIRMATION:
        # Not a missing answer: an answer was given and it was not the one the gate asks for.
        # Reporting it as missing would tell a driver to supply what it just supplied.
        raise InvalidArgument(
            "The deletion was not confirmed, so nothing was destroyed.",
            hint=(
                f"Type '{DELETE_CONFIRMATION}' when asked, or supply it with"
                f" --answer delete.confirm={DELETE_CONFIRMATION}."
            ),
            details={"environment": environment_name, "key": "delete.confirm"},
        )

    result = strategy_for(ctx, environment).destroy(deployment_config, environment)
    return reported(ctx, result)


#: Named so the module reads as the surface it is, and so an import of something private from it
#: stands out in review.
__all__ = [
    "CREATE_NEW_ENVIRONMENT",
    "ROUTED_SERVICE_TYPES",
    "bind_environment",
    "collect_domain_configuration",
    "create_environment",
    "declares_questions",
    "destroy_environment",
    "domain_questions",
    "environment_questions",
    "environment_status",
    "init_config",
    "is_deployed",
    "list_environments",
    "load_config",
    "must_be_an_email",
    "must_not_be_blank",
    "plan_environment",
    "release",
    "reported",
    "run_command",
    "run_setup",
    "select_environment",
    "services_needing_domains",
    "state_path",
    "strategy_for",
    "update",
]
