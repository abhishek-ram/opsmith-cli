"""The object the top level callback builds and every command reads.

Part 0b replaces this with ``OpsmithContext`` in ``opsmith/core/context.py``, once the core
modules take a context instead of printing for themselves.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from pydantic_ai import Agent

from opsmith.cli.output import BaseRenderer, OutputFormat


@dataclass
class CliState:
    """Everything the callback resolved, shared with every command through ``ctx.obj``."""

    src_dir: Path
    deployments_path: Path
    output: OutputFormat
    renderer: BaseRenderer
    verbose: bool = False
    agent: Optional[Agent] = None

    # Headless options. Parsed and carried here from part 0a so the command line surface is
    # stable; part 0e is what actually honours them.
    non_interactive: bool = False
    inline_answers: List[str] = field(default_factory=list)
    answers_file: Optional[Path] = None
    env_file: Optional[Path] = None
    accept_defaults: bool = False
    assume_yes: bool = False
    wait_timeout: int = 600
