"""Shared machinery for driving an external infrastructure tool.

The provisioners run a command in a working directory and report every line it writes as an
``output`` event, so the same run can be watched on a terminal or parsed from NDJSON. A command
that fails raises the caller's :class:`~opsmith.core.errors.OpsmithError` subclass rather than
a bare :class:`subprocess.CalledProcessError`, so the CLI can map it to an exit code.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Type

from opsmith.core.errors import OpsmithError
from opsmith.core.events import EventSink

#: How many trailing lines of a failed command's output travel in the error's details. Enough to
#: show what went wrong without putting a whole terraform plan into a JSON envelope.
OUTPUT_TAIL_LINES = 50

#: Matches the marker an ansible task prints to hand a value back to opsmith.
OUTPUT_MARKER_PATTERN = re.compile(r'"msg":\s*"OPSMITH_OUTPUT_(\w+)=([^"]*)"')


class BaseInfrastructureProvisioner:
    """
    Base class for provisioning infrastructure using specific commands or executables.

    Provides functionalities to handle command execution in a specified working directory.
    Ensures necessary directory setup and robust command execution with output streaming.

    :ivar working_dir: Directory where the commands will be executed.
    :ivar command_name: Name of the command/tool being executed (for user feedback).
    :ivar executable: Name or path of the executable/tool to be used for command execution.
    :ivar events: Sink the streamed output and progress are reported to.
    :ivar step: The step name every event from this provisioner carries.
    :ivar templates_dir: Root of the template tree :meth:`copy_template` reads from.
    :ivar error_class: The error raised when the tool fails.
    """

    def __init__(
        self,
        working_dir: Path,
        command_name: str,
        executable: str,
        events: EventSink,
        step: str,
        templates_dir: Path,
        error_class: Type[OpsmithError],
    ):
        self.working_dir = working_dir
        self.command_name = command_name
        self.executable = executable
        self.events = events
        self.step = step
        self.templates_dir = templates_dir
        self.error_class = error_class
        self.working_dir.mkdir(parents=True, exist_ok=True)

    def copy_template(self, template_name: str, provider: str):
        """
        Copies templates to the working directory.

        The provider is lower-cased here and nowhere else, because the template directories on
        disk are lower-case and callers pass a mix of ``name()`` and ``name().lower()``.

        :param template_name: The template tree to copy, such as "virtual_machine".
        :param provider: The cloud provider whose variant to copy, in any casing.
        :raises OpsmithError: The template directory does not exist.
        """
        template_dir = self.templates_dir / template_name / provider.lower()
        if not template_dir.exists() or not template_dir.is_dir():
            raise self.error_class(
                f"{self.command_name} templates for {provider.upper()} not found.",
                hint=(
                    "This provider does not ship a template for this step. Check the provider"
                    " plugin, or pick a provider that supports it."
                ),
                details={"template_dir": str(template_dir), "template": template_name},
            )

        # Use shutil.copytree to copy the contents of the template directory
        shutil.copytree(template_dir, self.working_dir, dirs_exist_ok=True)

        self.events.log(self.step, f"{self.command_name} files copied to: {self.working_dir}")

    def _run_command(
        self, command: List[str], env: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """
        Runs a command in the working directory and streams its output as events.

        :param command: The argument vector to run.
        :param env: Extra environment variables for the process.
        :return: The values the run handed back through ``OPSMITH_OUTPUT_`` markers.
        :raises OpsmithError: The executable is missing, or the command exited non-zero.
        """
        process_env = os.environ.copy()
        process_env.update(env or {})
        outputs = {}
        full_output = []

        try:
            process = subprocess.Popen(
                command,
                cwd=self.working_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                env=process_env,
            )
        except FileNotFoundError:
            raise self.error_class(
                f"'{self.executable}' command not found.",
                hint=(
                    f"Install {self.command_name} and make sure '{self.executable}' is on your"
                    " PATH."
                ),
                details={"executable": self.executable},
            )

        # Popen types stdout as Optional because it is None without a pipe; this call always
        # asks for one, so the stream is there.
        assert process.stdout is not None
        for line in iter(process.stdout.readline, ""):
            stripped_line = line.strip()
            full_output.append(stripped_line)
            self.events.output(self.step, stripped_line)
            match = OUTPUT_MARKER_PATTERN.search(stripped_line)
            if match:
                key = match.group(1).lower()
                value = match.group(2)
                outputs[key] = value

        process.wait()
        if process.returncode != 0:
            raise self.error_class(
                f"{self.command_name} command failed with exit code {process.returncode}.",
                hint=f"Re-run the command in {self.working_dir} to see the full output.",
                details={
                    "command": " ".join(command),
                    "working_dir": str(self.working_dir),
                    "returncode": process.returncode,
                    "output_tail": "\n".join(full_output[-OUTPUT_TAIL_LINES:]),
                },
            )

        return outputs
