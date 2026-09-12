"""The Typer application: global options, the callback, and the single error handler.

Everything that knows about a terminal lives under ``opsmith/cli/``. This module is where a
command's outcome becomes an exit code, and where an :class:`OpsmithError` becomes an error
envelope.
"""

import functools
import inspect
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import rich
import typer

from opsmith.cli.commands import analyze
from opsmith.cli.commands import config as config_commands
from opsmith.cli.commands import deploy, requirements_of, setup
from opsmith.cli.interaction import TerminalInteraction
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
from opsmith.core.context import OpsmithContext
from opsmith.core.errors import EXIT_CODES, InvalidArgument, OpsmithError
from opsmith.core.events import EventSink
from opsmith.core.llm import configure_agent, resolve_model_config
from opsmith.core.provisioners import ProvisionerFactory
from opsmith.deployment_strategies import DEPLOYMENT_STRATEGY_REGISTRY
from opsmith.models import MODEL_REGISTRY
from opsmith.settings import settings
from opsmith.utils import check_external_tools

app = typer.Typer(pretty_exceptions_show_locals=False)
config_app = typer.Typer(
    help="Inspect and validate the deployment configuration, without touching a cloud."
)


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


def _report_success(ctx: Optional[typer.Context], result: Optional[Dict] = None) -> None:
    """
    Renders the success envelope for a command that returned normally.

    :param ctx: The Typer context of the command that completed.
    :param result: What the command returned, if it returned a payload for the envelope.
        Part 0f gives every command a typed result; until then most return nothing.
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
    declared are working, and the run has a configured agent.

    :param func: The command function to wrap.
    :return: The wrapped function, with its signature preserved for Typer.
    """
    context_param = _context_param_name(func)
    tools = requirements_of(func)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        ctx: Optional[typer.Context] = kwargs.get(context_param) if context_param else None
        try:
            _ensure_external_tools(ctx, tools)
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

        _report_success(ctx, result if isinstance(result, dict) else None)
        return result

    return wrapper


@app.callback()
def main(
    ctx: typer.Context,
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
        help="Never prompt; answers must come from the answer options. Not yet implemented.",
    ),
    answer: Optional[List[str]] = typer.Option(
        None,
        "--answer",
        help="Inline answer as key=value. Repeatable. Not yet implemented.",
    ),
    answers: Optional[Path] = typer.Option(
        None,
        "--answers",
        help="YAML file mapping prompt key to value. Not yet implemented.",
    ),
    env_file: Optional[Path] = typer.Option(
        None,
        "--env-file",
        help="Dotenv file answering envvar.<KEY> prompts, secrets included. Not yet implemented.",
    ),
    accept_defaults: bool = typer.Option(
        False,
        "--accept-defaults",
        help="Take each question's default instead of failing on it. Not yet implemented.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Accept destructive confirmations. Not yet implemented.",
    ),
    wait_timeout: int = typer.Option(
        600,
        "--wait-timeout",
        help=(
            "Seconds a headless run polls an external action before giving up. Not yet implemented."
        ),
    ),
):
    """
    AI Devops engineer in your terminal.
    """
    # The renderer is built before anything that can fail, so every error has somewhere to go.
    # It is also the run's event sink, so progress and outcome go through one object.
    resolved_src_dir = Path(src_dir or os.getcwd())
    renderer = build_renderer(output)
    ctx.obj = CliState(
        context=OpsmithContext(
            src_dir=resolved_src_dir,
            deployments_path=resolved_src_dir.joinpath(settings.deployments_dir),
            events=renderer,
            provisioner_factory=ProvisionerFactory(events=renderer),
            interact=TerminalInteraction(renderer),
            verbose=verbose,
        ),
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
        assume_yes=yes,
        wait_timeout=wait_timeout,
    )

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
    except typer.Exit as exit_exc:
        _report_passthrough_exit(ctx, exit_exc.exit_code)
        raise
    except typer.Abort:
        raise
    except OpsmithError as err:
        raise _report_error(ctx, err, traceback.format_exc() if verbose else None)
    except Exception as err:
        raise _report_unexpected(ctx, err)


app.command()(handle_errors(setup.setup))
app.command()(handle_errors(deploy.deploy))
app.command()(handle_errors(analyze.repomap))

config_app.command("validate")(handle_errors(config_commands.validate))
config_app.command("schema")(handle_errors(config_commands.schema))
config_app.command("show")(handle_errors(config_commands.show))
app.add_typer(config_app, name="config")
