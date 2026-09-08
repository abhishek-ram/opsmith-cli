"""The object the top level callback builds and every command reads.

This holds what only a terminal cares about: which renderer is writing, which output mode was
asked for, and the headless options part 0e turns into answer sources. Everything a core module
needs lives on the :class:`~opsmith.core.context.OpsmithContext` it carries, which is the only
half a strategy or a detector ever sees.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from opsmith.cli.output import BaseRenderer, OutputFormat
from opsmith.core.context import OpsmithContext


@dataclass
class CliState:
    """Everything the callback resolved, shared with every command through ``ctx.obj``."""

    context: OpsmithContext
    output: OutputFormat
    renderer: BaseRenderer
    verbose: bool = False

    # The model options exactly as they were given. They are resolved into the context's agent
    # just before a command body runs, rather than in the callback, so that reading a
    # subcommand's --help never requires a configured model.
    model: Optional[str] = None
    api_key: Optional[str] = None
    logfire_token: Optional[str] = None

    # Headless options. Parsed and carried here from part 0a so the command line surface is
    # stable; part 0e is what actually honours them.
    non_interactive: bool = False
    inline_answers: List[str] = field(default_factory=list)
    answers_file: Optional[Path] = None
    env_file: Optional[Path] = None
    accept_defaults: bool = False
    assume_yes: bool = False
    wait_timeout: int = 600

    @property
    def src_dir(self) -> Path:
        """The repository the run operates on."""
        return self.context.src_dir

    @property
    def deployments_path(self) -> Path:
        """The ``.opsmith`` directory inside it."""
        return self.context.deployments_path

    @property
    def agent(self):
        """The configured model."""
        return self.context.agent
