"""The Typer application: global options, the callback, and the single error handler.

Everything that knows about a terminal lives under ``opsmith/cli/``. This module is where a
command's outcome becomes an exit code, and where an :class:`OpsmithError` becomes an error
envelope.
"""

import functools
import inspect
import os
import shlex
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import rich
import typer
from pydantic import BaseModel

from opsmith.cli.commands import agent as agent_commands
from opsmith.cli.commands import analyze
from opsmith.cli.commands import config as config_commands
from opsmith.cli.commands import deploy
from opsmith.cli.commands import env as env_commands
from opsmith.cli.commands import needs_model
from opsmith.cli.commands import release as release_commands
from opsmith.cli.commands import requirements_of, setup
from opsmith.cli.commands import validate as validate_commands
from opsmith.cli.flags import build_interaction
from opsmith.cli.output import (
    BaseRenderer,
    OutputFormat,
    TextRenderer,
    build_logo,
    build_renderer,
    command_name,
)
from opsmith.cli.state import CliState
from opsmith.cloud_providers import CLOUD_PROVIDER_REGISTRY
from opsmith.core.answers import AnswerSources, AnswerStore
from opsmith.core.context import OpsmithContext
from opsmith.core.errors import EXIT_CODES, InvalidArgument, OpsmithError
from opsmith.core.events import EventSink
from opsmith.core.interaction import HeadlessInteraction
from opsmith.core.llm import configure_agent, resolve_model_config
from opsmith.core.provisioners import ProvisionerFactory
from opsmith.deployment_strategies import DEPLOYMENT_STRATEGY_REGISTRY
from opsmith.models import MODEL_REGISTRY
from opsmith.settings import settings
from opsmith.utils import check_external_tools, package_version, project_state_dir

app = typer.Typer(pretty_exceptions_show_locals=False)
config_app = typer.Typer(
    help="Inspect and validate the deployment configuration, without touching a cloud."
)
env_app = typer.Typer(help="Inspect and create the deployment environments of this repository.")
dockerfile_app = typer.Typer(help="Check the Dockerfiles this repository declares.")
agent_app = typer.Typer(help="Install the Opsmith skill into your coding harness.")


#: Options whose value is a credential. They are dropped from the resume command, which is
#: reported in an error envelope and read by whatever is driving the run.
SECRET_OPTIONS: Set[str] = {"--api-key", "--logfire-token"}


def _resume_command(argv: List[str]) -> str:
    """
    Describes the invocation that is running, so a stop can say how to continue it.

    Everything the run was told is preserved, because the point of the resume command is that it
    carries the answers already supplied; the credentials are the exception, since the string ends
    up in an error envelope on stdout.

    :param argv: The process argument vector.
    :return: The command to run again, quoted for a shell.
    """
    if not argv:
        return "opsmith"

    parts = [Path(argv[0]).name]
    drop_value = False
    for token in argv[1:]:
        if drop_value:
            drop_value = False
            continue
        if token.split("=", 1)[0] in SECRET_OPTIONS:
            drop_value = "=" not in token
            continue
        parts.append(token)

    return shlex.join(parts)


def _is_headless(non_interactive: bool, output: OutputFormat, stdin: Any) -> bool:
    """
    Decides whether this run has anybody to ask.

    JSON output counts as headless whatever the terminal says, because a prompt draws on stdout
    and stdout in that mode carries exactly one envelope and nothing else.

    :param non_interactive: Whether --non-interactive or OPSMITH_NON_INTERACTIVE was given.
    :param output: The output mode.
    :param stdin: The run's standard input, asked whether it is a terminal.
    :return: Whether to resolve answers instead of prompting.
    """
    return non_interactive or output is OutputFormat.JSON or not stdin.isatty()


def _drain_registry_events(events: EventSink):
    """
    Replays what the plugin registries recorded while they loaded.

    The registries are module level singletons that load their entry points at import time, long
    before there is a renderer to report through, so they buffer and this hands the buffer on.

    :param events: The sink to replay into.
    """
    for registry in (MODEL_REGISTRY, CLOUD_PROVIDER_REGISTRY, DEPLOYMENT_STRATEGY_REGISTRY):
        registry.pending_events.drain_into(events)


def _configure_logfire(token: str):
    """
    Turns on Logfire tracing for this run.

    Logfire is an optional extra, so it is imported here rather than at the top of the module: a
    user who never passes a token never needs it installed.

    :param token: The Logfire token to report with.
    :raises InvalidArgument: The extra is not installed.
    """
    try:
        import logfire
    except ImportError as err:
        raise InvalidArgument(
            "Logfire tracing was requested but logfire is not installed.",
            hint='Install it with: pip install "opsmith-cli[logfire]"',
        ) from err

    logfire.configure(token=token, scrubbing=False)


def _state_of(ctx: Optional[typer.Context]) -> Optional[CliState]:
    """
    Returns the state the callback built, if it got far enough to build one.

    :param ctx: The Typer context of the running command, or None.
    :return: The shared state, or None when the run failed before the callback stored it.
    """
    state = ctx.obj if ctx is not None else None
    return state if isinstance(state, CliState) else None


def _renderer_of(ctx: Optional[typer.Context]) -> BaseRenderer:
    """
    Returns the renderer for this invocation, falling back to text output.

    The fallback matters when a failure happens before the callback picked a renderer,
    because an error still has to reach the user.

    :param ctx: The Typer context of the running command, or None.
    :return: The renderer to report through.
    """
    state = _state_of(ctx)
    return state.renderer if state is not None else TextRenderer()


def _envelope_result(result: Any) -> Optional[Dict]:
    """
    Shapes what a command returned into the envelope's ``result`` field.

    :param result: What the command body returned.
    :return: The payload, or None when the command returned nothing to report.
    """
    if isinstance(result, BaseModel):
        # by_alias, because a field whose Python name cannot be what the envelope calls it says so
        # with a serialization alias. ConfigSchemaResult.schema_document is the one today: a field
        # named ``schema`` would shadow a method of BaseModel.
        return result.model_dump(mode="json", by_alias=True)
    if isinstance(result, dict):
        return result
    return None


def _report_success(ctx: Optional[typer.Context], result: Optional[Dict] = None) -> None:
    """
    Renders the success envelope for a command that returned normally.

    :param ctx: The Typer context of the command that completed.
    :param result: What the command returned, if it returned a payload for the envelope.
    """
    _renderer_of(ctx).render_success(command_name(ctx), result=result)


def _report_error(
    ctx: Optional[typer.Context],
    error: OpsmithError,
    traceback_text: Optional[str] = None,
) -> typer.Exit:
    """
    Renders an error and returns the exit the caller should raise.

    :param ctx: The Typer context of the command that failed.
    :param error: The error to report.
    :param traceback_text: A formatted traceback, included only under --verbose.
    :return: A ``typer.Exit`` carrying the exit code mapped from the error's code.
    """
    _renderer_of(ctx).render_error(command_name(ctx), error, traceback_text=traceback_text)
    return typer.Exit(code=EXIT_CODES[error.code])


def _report_unexpected(ctx: Optional[typer.Context], error: Exception) -> typer.Exit:
    """
    Reports an exception Opsmith did not anticipate as INTERNAL, exit code 1.

    :param ctx: The Typer context of the command that failed.
    :param error: The exception that escaped the command.
    :return: A ``typer.Exit`` carrying exit code 1.
    """
    internal = OpsmithError(
        f"{type(error).__name__}: {error}",
        hint="This is a bug in opsmith. Re-run with --verbose to see the traceback.",
    )
    state = _state_of(ctx)
    traceback_text = traceback.format_exc() if state is not None and state.verbose else None
    return _report_error(ctx, internal, traceback_text=traceback_text)


def _report_passthrough_exit(ctx: Optional[typer.Context], exit_code: int) -> None:
    """
    Renders an envelope for a ``typer.Exit`` raised inside a command body.

    A zero exit is a success; anything else is reported as INTERNAL, because the command
    aborted without describing why. Part 0f replaces those abort paths with typed results.

    :param ctx: The Typer context of the command that exited.
    :param exit_code: The code the command asked to exit with.
    """
    if exit_code == 0:
        _report_success(ctx)
        return

    _renderer_of(ctx).render_aborted(command_name(ctx), exit_code)


def _ensure_external_tools(ctx: Optional[typer.Context], tools: Tuple[str, ...]):
    """
    Checks the external tools a command declared, before its body runs.

    A command that declared none is not checked at all, which is what lets ``config validate``
    run on a machine with neither docker nor terraform installed.

    :param ctx: The Typer context of the command about to run.
    :param tools: The tools the command declared through ``@requires``.
    :raises InvalidArgument: One of them is missing or not working.
    """
    if not tools:
        return

    report = check_external_tools(tools)
    if report.missing:
        raise InvalidArgument(
            f"Required dependencies not found or not running: {', '.join(report.missing)}.",
            hint="Please install them and ensure they are in your system's PATH.",
            details={"missing": report.missing},
        )

    state = _state_of(ctx)
    if state is not None and "terraform" in report.versions:
        state.context.terraform_version = report.versions["terraform"]


def _prepare_agent(ctx: Optional[typer.Context]):
    """
    Resolves the model configuration and builds the agent, once, before a command body runs.

    This happens here rather than in the callback because click runs the group callback before it
    reaches a subcommand's ``--help``: resolving there would mean ``opsmith setup --help`` insists
    on the very configuration the help is there to explain.

    :param ctx: The Typer context of the command about to run.
    :raises InvalidArgument: No usable model or API key was configured.
    """
    state = _state_of(ctx)
    if state is None or state.context.agent is not None:
        return

    if state.logfire_token:
        _configure_logfire(state.logfire_token)

    # Resolved in one place rather than in an option callback, so that the order of --model and
    # --api-key does not matter and a bad value is an OpsmithError like any other.
    model_config = resolve_model_config(state.model, state.api_key)
    state.context.agent = configure_agent(model_config, instrument=bool(state.logfire_token))


def _context_param_name(func: Callable) -> Optional[str]:
    """
    Finds the name of the parameter Typer fills with the context.

    Typer vendors its own click, so there is no ambient context to read; the context only
    arrives as an argument, under whatever name the command declared it with.

    :param func: The command function being wrapped.
    :return: The parameter name, or None if the command does not take a context.
    """
    for name, parameter in inspect.signature(func).parameters.items():
        if parameter.annotation is typer.Context:
            return name
    return None


def handle_errors(func: Callable) -> Callable:
    """
    Wraps a command body so its outcome becomes exactly one envelope and one exit code.

    It also holds what has to be true before a body runs: the external tools the command
    declared are working, and the run has a configured agent unless the command declared it needs
    none.

    :param func: The command function to wrap.
    :return: The wrapped function, with its signature preserved for Typer.
    """
    context_param = _context_param_name(func)
    tools = requirements_of(func)
    model_needed = needs_model(func)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        ctx: Optional[typer.Context] = kwargs.get(context_param) if context_param else None
        try:
            _ensure_external_tools(ctx, tools)
            if model_needed:
                _prepare_agent(ctx)
            result = func(*args, **kwargs)
        except typer.Exit as exit_exc:
            _report_passthrough_exit(ctx, exit_exc.exit_code)
            raise
        except typer.Abort:
            raise
        except OpsmithError as err:
            state = _state_of(ctx)
            verbose = state is not None and state.verbose
            raise _report_error(ctx, err, traceback.format_exc() if verbose else None)
        except Exception as err:
            raise _report_unexpected(ctx, err)

        _report_success(ctx, _envelope_result(result))

        # One command ends in a code of its own: `opsmith run` exits with what the remote command
        # exited with. It happens after the envelope, and outside the guard above, so the success
        # it just reported is the only envelope written.
        exit_code = getattr(result, "process_exit_code", 0)
        if exit_code:
            raise typer.Exit(code=exit_code)

        return result

    return wrapper


def _report_version(value: bool) -> None:
    """
    Prints the version and stops, before anything else is resolved.

    It is eager so that ``opsmith --version`` answers on a machine with no model configured and no
    repository to read - a harness asks this first, to know which release's instructions it holds.

    :param value: Whether --version was given.
    :raises typer.Exit: Always, when it was.
    """
    if value:
        typer.echo(package_version())
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=_report_version,
        is_eager=True,
        help="Print the version of Opsmith and exit.",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help=(
            "The LLM model to be used by the AI Agent, as provider:name. Required unless"
            " OPSMITH_MODEL is set or .opsmith.conf.yml names one."
        ),
    ),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        help=(
            "The API key for the specified model. Required unless the provider's own key"
            " variable, such as ANTHROPIC_API_KEY, is set."
        ),
    ),
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
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output."),
    output: OutputFormat = typer.Option(
        OutputFormat.TEXT,
        "--output",
        help=(
            "Output mode. 'text' is the usual terminal output; 'json' prints exactly one JSON"
            " envelope on stdout and sends all progress to stderr."
        ),
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        envvar="OPSMITH_NON_INTERACTIVE",
        help=(
            "Never prompt; answers come from the answer options, and a question none of them"
            " covers stops the run. Implied by --output json and by a stdin that is not a"
            " terminal."
        ),
    ),
    answer: Optional[List[str]] = typer.Option(
        None,
        "--answer",
        help="Inline answer as key=value. Repeatable, and wins over every other source.",
    ),
    answers: Optional[Path] = typer.Option(
        None,
        "--answers",
        help="YAML file mapping prompt key to value.",
    ),
    env_file: Optional[Path] = typer.Option(
        None,
        "--env-file",
        help="Dotenv file answering envvar.<KEY> prompts, secrets included.",
    ),
    accept_defaults: bool = typer.Option(
        False,
        "--accept-defaults",
        help=(
            "Take each question's default instead of stopping on it. Destructive"
            " confirmations are excluded."
        ),
    ),
    wait_timeout: int = typer.Option(
        600,
        "--wait-timeout",
        help=(
            "Seconds a headless run polls an external action, such as a DNS record being"
            " created, before stopping so it can be done."
        ),
    ),
):
    """
    AI Devops engineer in your terminal.
    """
    # The renderer is built before anything that can fail, so every error has somewhere to go.
    # It is also the run's event sink, so progress and outcome go through one object.
    resolved_src_dir = Path(src_dir or os.getcwd())
    deployments_path = resolved_src_dir.joinpath(settings.deployments_dir)
    renderer = build_renderer(output)

    # One store, shared by the context and by the interaction built against it. Binding it to an
    # environment later mutates it in place, which is how a question asked before the environment
    # is known still ends up in that environment's file.
    answer_store = AnswerStore(project_state_dir(deployments_path), events=renderer)
    resume = _resume_command(sys.argv)
    headless = _is_headless(non_interactive, output, sys.stdin)

    # Reading the answer options can fail, and an error has to render through the renderer this
    # run actually chose. So the context starts with an interaction that cannot fail - a headless
    # one told nothing - and the configured one replaces it inside the guarded block below.
    context = OpsmithContext(
        src_dir=resolved_src_dir,
        deployments_path=deployments_path,
        events=renderer,
        provisioner_factory=ProvisionerFactory(events=renderer),
        interact=HeadlessInteraction(AnswerSources(), answer_store, events=renderer, resume=resume),
        verbose=verbose,
        answers=answer_store,
    )
    state = CliState(
        context=context,
        output=output,
        renderer=renderer,
        verbose=verbose,
        model=model,
        api_key=api_key,
        logfire_token=logfire_token,
        non_interactive=non_interactive,
        inline_answers=list(answer or []),
        answers_file=answers,
        env_file=env_file,
        accept_defaults=accept_defaults,
        wait_timeout=wait_timeout,
        headless=headless,
        resume=resume,
    )
    ctx.obj = state

    if output is OutputFormat.JSON:
        # The core reports through events now, but the command modules under opsmith/cli/ still
        # print directly until part 0d moves their prompts. rich resolves its global console per
        # call, so this moves those prints to stderr and leaves stdout for the envelope alone.
        # `stderr=True` rather than `file=sys.stderr` so the stream is looked up per write.
        rich.reconfigure(stderr=True)

    try:
        if output is OutputFormat.TEXT and sys.stdout.isatty():
            renderer.render_logo(build_logo())

        _drain_registry_events(renderer)

        state.sources = AnswerSources.load(
            inline=answer,
            answers_path=answers,
            env_path=env_file,
            accept_defaults=accept_defaults,
        )
        context.interact = build_interaction(
            renderer,
            state.sources,
            answer_store,
            headless=headless,
            wait_timeout=wait_timeout,
            resume=resume,
        )
    except typer.Exit as exit_exc:
        _report_passthrough_exit(ctx, exit_exc.exit_code)
        raise
    except typer.Abort:
        raise
    except OpsmithError as err:
        raise _report_error(ctx, err, traceback.format_exc() if verbose else None)
    except Exception as err:
        raise _report_unexpected(ctx, err)


app.command()(handle_errors(setup.init))
app.command()(handle_errors(setup.setup))
app.command()(handle_errors(deploy.deploy))
app.command()(handle_errors(analyze.repomap))
app.command("release")(handle_errors(release_commands.release))
app.command("update")(handle_errors(release_commands.update))
app.command("run")(handle_errors(release_commands.run))
app.command("destroy")(handle_errors(release_commands.destroy))

config_app.command("validate")(handle_errors(config_commands.validate))
config_app.command("schema")(handle_errors(config_commands.schema))
config_app.command("show")(handle_errors(config_commands.show))
app.add_typer(config_app, name="config")

env_app.command("list")(handle_errors(env_commands.list_environments))
env_app.command("plan")(handle_errors(env_commands.plan))
env_app.command("create")(handle_errors(env_commands.create))
env_app.command("status")(handle_errors(env_commands.status))
app.add_typer(env_app, name="env")

dockerfile_app.command("validate")(handle_errors(validate_commands.validate_dockerfile))
app.add_typer(dockerfile_app, name="dockerfile")

agent_app.command("install")(handle_errors(agent_commands.install))
agent_app.command("uninstall")(handle_errors(agent_commands.uninstall))
agent_app.command("status")(handle_errors(agent_commands.status))
app.add_typer(agent_app, name="agent")
