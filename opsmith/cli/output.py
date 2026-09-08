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
from rich.console import Console
from rich.markup import escape
from rich.status import Status
from rich.text import Text

from opsmith.core.errors import OpsmithError
from opsmith.core.events import STATUS_FINISHED, STATUS_STARTED, Event, EventSink


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


def build_logo() -> Text:
    """
    Builds and returns an ASCII art logo styled with specific colors and text formats.
    The function creates a stylized representation of a logo using the ``Text`` object.
    Each line of the logo is appended to the text object with a distinct style, alternating
    between bold cyan and bold blue.

    :return: Styled ASCII art logo representation.
    :rtype: Text
    """
    ascii_art_logo = Text()
    ascii_art_logo.append(
        (
            "\n \u2588\u2588\u2588\u2588\u2588\u2588  \u2588\u2588\u2588\u2588\u2588\u2588 "
            " \u2588\u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588\u2588    \u2588\u2588\u2588"
            " \u2588\u2588 \u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588  "
            " \u2588\u2588\n"
        ),
        style="bold cyan",
    )
    ascii_art_logo.append(
        (
            "\u2588\u2588    \u2588\u2588 \u2588\u2588   \u2588\u2588 \u2588\u2588     "
            " \u2588\u2588\u2588\u2588  \u2588\u2588\u2588\u2588 \u2588\u2588    \u2588\u2588   "
            " \u2588\u2588   \u2588\u2588\n"
        ),
        style="bold blue",
    )
    ascii_art_logo.append(
        (
            "\u2588\u2588    \u2588\u2588 \u2588\u2588\u2588\u2588\u2588\u2588 "
            " \u2588\u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588 \u2588\u2588\u2588\u2588"
            " \u2588\u2588 \u2588\u2588    \u2588\u2588   "
            " \u2588\u2588\u2588\u2588\u2588\u2588\u2588\n"
        ),
        style="bold cyan",
    )
    ascii_art_logo.append(
        (
            "\u2588\u2588    \u2588\u2588 \u2588\u2588           \u2588\u2588 \u2588\u2588 "
            " \u2588\u2588  \u2588\u2588 \u2588\u2588    \u2588\u2588    \u2588\u2588  "
            " \u2588\u2588\n"
        ),
        style="bold blue",
    )
    ascii_art_logo.append(
        (
            " \u2588\u2588\u2588\u2588\u2588\u2588  \u2588\u2588     "
            " \u2588\u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588      \u2588\u2588"
            " \u2588\u2588    \u2588\u2588    \u2588\u2588   \u2588\u2588\n\n"
        ),
        style="bold cyan",
    )
    return ascii_art_logo


class BaseRenderer(EventSink):
    """Writes the progress and the outcome of a single command invocation.

    A renderer is the run's event sink as well as its reporter, so everything the user sees goes
    through one object and one stream.
    """

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

    def render_logo(self, logo: Text):
        """
        Shows the banner an interactive run opens with. Only the text renderer has one.

        :param logo: The styled banner.
        """


class TextRenderer(BaseRenderer):
    """Prints what Opsmith printed before the core stopped printing for itself.

    Styling lives here rather than in the events, so an ``Event.message`` stays plain text that
    reads as well in a JSON envelope as it does on a terminal.
    """

    #: How each kind of event is styled. A ``step`` event is a heading; the pair that brackets a
    #: wait is handled separately, as a spinner.
    STYLES = {
        "step": "bold blue",
        "log": None,
        "warning": "bold yellow",
        "output": "grey50",
    }

    def __init__(self, console: Optional[Console] = None):
        """
        :param console: The console to write through. One console for both the spinner and the
            lines it brackets, because two consoles on one stream interleave badly.
        """
        self.console = console if console is not None else Console()
        self._status: Optional[Status] = None

    def emit(self, event: Event):
        """
        Renders one event, or starts and stops the spinner that brackets a wait.

        :param event: The event to render.
        """
        status = event.data.get("status")
        if event.kind == "step" and status == STATUS_STARTED:
            self._start_waiting(event.message)
            return
        if event.kind == "step" and status == STATUS_FINISHED:
            self._stop_waiting()
            return

        # Every message is escaped, not just subprocess output: an event's message is plain
        # text, and a terraform resource address or a model's reasoning can contain brackets
        # that rich would otherwise read as markup.
        message = escape(event.message)
        style = self.STYLES.get(event.kind)
        if event.kind == "step":
            message = f"\n{message}"

        self.console.print(f"[{style}]{message}[/{style}]" if style else message)

    def _start_waiting(self, message: str):
        """
        Shows a spinner for a slow operation.

        :param message: What is being waited for.
        """
        self._stop_waiting()
        self._status = self.console.status(message)
        self._status.start()

    def _stop_waiting(self):
        """Clears the spinner, if one is running."""
        if self._status is not None:
            self._status.stop()
            self._status = None

    def render_logo(self, logo: Text):
        """
        Prints the banner shown at the top of an interactive run.

        :param logo: The styled banner.
        """
        self.console.print(logo)

    def render_success(
        self,
        command: str,
        result: Optional[Dict] = None,
        warnings: Optional[List[str]] = None,
    ):
        """Prints any warnings. Commands print their own success messages today."""
        self._stop_waiting()
        for warning in warnings or []:
            self.console.print(f"[yellow]{warning}[/yellow]")

    def render_error(
        self,
        command: str,
        error: OpsmithError,
        traceback_text: Optional[str] = None,
    ):
        """Prints the error message in red, followed by its hint and optional traceback."""
        self._stop_waiting()
        self.console.print(f"[bold red]{error.message}[/bold red]")
        if error.hint:
            self.console.print(f"[yellow]{error.hint}[/yellow]")
        if traceback_text:
            self.console.print(f"[dim]{traceback_text}[/dim]")

    def render_aborted(self, command: str, exit_code: int):
        """
        Prints nothing beyond clearing the spinner. A command that exits without an error has
        already said its piece, and restating it as "exited with code N" is noise a terminal user
        never used to see.
        """
        self._stop_waiting()


class JsonRenderer(BaseRenderer):
    """Writes exactly one JSON envelope to stdout, at the end of the run."""

    def __init__(self):
        self._finished = False

    def emit(self, event: Event):
        """
        Streams one event to stderr as NDJSON, leaving stdout for the envelope alone.

        :param event: The event to stream.
        """
        sys.stderr.write(json.dumps(event.model_dump()) + "\n")
        sys.stderr.flush()

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
