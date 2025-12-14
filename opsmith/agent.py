import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Type

from pydantic_ai import Agent, ModelRetry, RunContext

from opsmith.models import BaseAiModel
from opsmith.prompts import SYSTEM_PROMPT
from opsmith.utils import generate_secret_string


@dataclass
class AgentDeps:
    src_dir: Path
    tracked_files: List[Path] = field(default_factory=list)


def is_duplicate_tool_call(ctx: RunContext[AgentDeps], tool_name: str) -> bool:
    """"""
    tool_calls = set()
    message_parts = [item for message in ctx.messages for item in message.parts]
    for part in message_parts:
        if part.part_kind == "tool-call" and part.tool_name == tool_name:
            if isinstance(part.args, dict):
                tool_args = json.dumps(part.args, sort_keys=True)
            else:
                tool_args = part.args
            if tool_args in tool_calls:
                return True
            else:
                # logger.debug(f"Tool {tool_def.name} called with arguments: {tool_args}")
                tool_calls.add(tool_args)

    return False


def build_agent(model_config: Type[BaseAiModel], instrument: bool = False) -> Agent:
    agent = Agent(
        model=model_config.model_name_abs(),
        model_settings=model_config.get_model_settings(),
        instructions=SYSTEM_PROMPT,
        instrument=instrument,
        deps_type=AgentDeps,
    )

    @agent.tool(retries=5)
    def read_repo_files(ctx: RunContext[AgentDeps], filenames: List[str]) -> List[str]:
        """
        Reads and returns the content of specified files from the repo.
        Use this to understand file structures, dependencies, or specific configurations.
        Provide the relative file paths from the repository root.

        Args:
            ctx: The run context object containing the dependencies of the agent.
            filenames: A list of relative paths to the files from the repository root.

        Returns:
            A list of strings, where each string is the content of the corresponding file.
            The order of contents in the list matches the order of filenames in the input.
        """
        if is_duplicate_tool_call(ctx, "read_repo_files"):
            raise ModelRetry(
                "The tool 'read_repo_files' has already been called with the exact same list of "
                "files in this conversation."
            )

        allowed_files_abs = {str(p) for p in ctx.deps.tracked_files}
        contents = []
        for filename in filenames:
            if Path(filename).is_absolute():
                raise ModelRetry(
                    f"Absolute file paths are not allowed for '{filename}'. Please provide a"
                    " relative path."
                )

            absolute_file_path = ctx.deps.src_dir.joinpath(filename).resolve()

            if str(absolute_file_path) not in allowed_files_abs:
                raise ModelRetry(f"File '{filename}' is not in the repo map and cannot be read.")

            if not str(absolute_file_path).startswith(str(ctx.deps.src_dir)):
                raise ModelRetry(
                    f"Access denied. File '{filename}' is outside the repository root."
                )

            if not absolute_file_path.is_file():
                raise ModelRetry(f"File '{filename}' not found or is not a regular file.")

            with open(absolute_file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            contents.append(content)
        return contents

    @agent.tool()
    def generate_secret(ctx: RunContext[AgentDeps], length: int = 32) -> str:
        """
        Generates a secure random string of a specified length.
        Useful for creating passwords, API keys, or other secrets.

        Args:
            ctx: The run context object.
            length: The desired length of the secret string. Defaults to 32.

        Returns:
            A secure random string.
        """
        return generate_secret_string(length)

    return agent
