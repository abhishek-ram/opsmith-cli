"""Renderers that turn a command's outcome into terminal output or a JSON envelope.

``TextRenderer`` reproduces what Opsmith has always printed. ``JsonRenderer`` writes exactly
one document to stdout, which is the contract a coding harness drives Opsmith against.
"""

import abc
import json
import sys
from enum import Enum
from typing import Dict, List, Optional

import typer
from rich import print

from opsmith.core.errors import OpsmithError


class OutputFormat(str, Enum):
    """The two output modes every command supports."""

    TEXT = "text"
    JSON = "json"


def command_name(ctx: Optional[typer.Context]) -> str:
    """
    Names the command being invoked, without the program name in front of it.

    Typer vendors its own copy of click, so there is no reliable ambient context to read;
    the context is always passed in explicitly by the caller that has it.

    :param ctx: The Typer context of the running command, or None if there is not one.
    :return: A command name such as "setup", or the program name when the failure happened
        in the top level callback before a subcommand was resolved.
    """
    if ctx is None:
        return ""

    parts = ctx.command_path.split()
    if len(parts) > 1:
        return " ".join(parts[1:])
    return ctx.info_name or ""


class BaseRenderer(abc.ABC):
    """Writes the outcome of a single command invocation."""

    @abc.abstractmethod
    def render_success(
        self,
        command: str,
        result: Optional[Dict] = None,
        warnings: Optional[List[str]] = None,
    ):
        """
        Reports that the command completed.

        :param command: The name of the command that ran.
        :param result: The typed result of the command. Empty until part 0f defines them.
        :param warnings: Non-fatal messages collected during the run.
        """

    @abc.abstractmethod
    def render_error(
        self,
        command: str,
        error: OpsmithError,
        traceback_text: Optional[str] = None,
    ):
        """
        Reports that the command failed.

        :param command: The name of the command that ran.
        :param error: The error to report.
        :param traceback_text: A formatted traceback, included only when --verbose is set.
        """

    @abc.abstractmethod
    def render_aborted(self, command: str, exit_code: int):
        """
        Reports a command that bailed out with a non-zero exit but no error to describe it.

        :param command: The name of the command that ran.
        :param exit_code: The code the command asked to exit with.
        """


class TextRenderer(BaseRenderer):
    """Prints what Opsmith printed before the CLI split, using rich."""

    def render_success(
        self,
        command: str,
        result: Optional[Dict] = None,
        warnings: Optional[List[str]] = None,
    ):
        """Prints any warnings. Commands print their own success messages today."""
        for warning in warnings or []:
            print(f"[yellow]{warning}[/yellow]")

    def render_error(
        self,
        command: str,
        error: OpsmithError,
        traceback_text: Optional[str] = None,
    ):
        """Prints the error message in red, followed by its hint and optional traceback."""
        print(f"[bold red]{error.message}[/bold red]")
        if error.hint:
            print(f"[yellow]{error.hint}[/yellow]")
        if traceback_text:
            print(f"[dim]{traceback_text}[/dim]")

    def render_aborted(self, command: str, exit_code: int):
        """
        Prints nothing. A command that exits without an error has already said its piece, and
        restating it as "exited with code N" is noise a terminal user never used to see.
        """


class JsonRenderer(BaseRenderer):
    """Writes exactly one JSON envelope to stdout, at the end of the run."""

    def __init__(self):
        self._finished = False

    def render_success(
        self,
        command: str,
        result: Optional[Dict] = None,
        warnings: Optional[List[str]] = None,
    ):
        """Writes the success envelope, unless an envelope has already been written."""
        self._write(
            {
                "ok": True,
                "command": command,
                "result": result if result is not None else {},
                "warnings": warnings if warnings is not None else [],
            }
        )

    def render_error(
        self,
        command: str,
        error: OpsmithError,
        traceback_text: Optional[str] = None,
    ):
        """Writes the error envelope, unless an envelope has already been written."""
        details = dict(error.details)
        if traceback_text:
            details["traceback"] = traceback_text

        self._write(
            {
                "ok": False,
                "command": command,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "hint": error.hint,
                    "details": details,
                },
            }
        )

    def render_aborted(self, command: str, exit_code: int):
        """Writes an INTERNAL envelope, because a harness still needs a machine-readable
        reason for the non-zero exit."""
        self.render_error(command, OpsmithError(f"Command exited with code {exit_code}."))

    def _write(self, envelope: Dict):
        """Writes a single envelope to stdout and latches, so only the first one is emitted."""
        if self._finished:
            return

        self._finished = True
        sys.stdout.write(json.dumps(envelope) + "\n")
        sys.stdout.flush()


def build_renderer(output: OutputFormat) -> BaseRenderer:
    """
    Returns the renderer for the requested output mode.

    :param output: The value of the global --output option.
    :return: A renderer that writes text or a JSON envelope.
    """
    if output is OutputFormat.JSON:
        return JsonRenderer()
    return TextRenderer()
