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
from typing import Annotated, Any, Callable, Dict, List, Optional, Type, Union

import logfire
import rich
import typer
from rich import print

from opsmith.agent import build_agent
from opsmith.cli.commands import analyze, deploy, setup
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
from opsmith.core.errors import EXIT_CODES, OpsmithError
from opsmith.core.events import EventSink
from opsmith.core.provisioners import ProvisionerFactory
from opsmith.deployment_strategies import DEPLOYMENT_STRATEGY_REGISTRY
from opsmith.models import MODEL_REGISTRY, BaseAiModel
from opsmith.settings import settings
from opsmith.utils import get_missing_external_dependencies

app = typer.Typer(pretty_exceptions_show_locals=False)


def _drain_registry_events(events: EventSink):
    """
    Replays what the plugin registries recorded while they loaded.

    The registries are module level singletons that load their entry points at import time, long
    before there is a renderer to report through, so they buffer and this hands the buffer on.

    :param events: The sink to replay into.
    """
    for registry in (MODEL_REGISTRY, CLOUD_PROVIDER_REGISTRY, DEPLOYMENT_STRATEGY_REGISTRY):
        registry.pending_events.drain_into(events)


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

    :param func: The command function to wrap.
    :return: The wrapped function, with its signature preserved for Typer.
    """
    context_param = _context_param_name(func)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        ctx: Optional[typer.Context] = kwargs.get(context_param) if context_param else None
        try:
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
            verbose=verbose,
        ),
        output=output,
        renderer=renderer,
        verbose=verbose,
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

        if logfire_token:
            logfire.configure(token=logfire_token, scrubbing=False)

        ctx.obj.context.agent = build_agent(model_config=model, instrument=bool(logfire_token))
        _check_external_dependencies()
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
