"""``opsmith config validate|schema|show``: everything a harness can ask about a repository
without a cloud account, docker or terraform.

None of these commands declares an external tool, so they run anywhere the package is installed.
"""

import json
from enum import Enum
from pathlib import Path
from typing import Optional

import typer
import yaml

from opsmith.cli.state import CliState
from opsmith.core.config import (
    config_json_schema,
    load_config_file,
    schema_to_markdown,
    validate_config_file,
)
from opsmith.core.errors import InvalidConfig
from opsmith.core.events import STEP_CONFIG
from opsmith.core.operations import reported
from opsmith.core.results import ConfigSchemaResult, ConfigShowResult, ValidateResult
from opsmith.settings import settings


class SchemaFormat(str, Enum):
    """The two renderings of the configuration schema."""

    JSON = "json"
    MARKDOWN = "markdown"


def _config_path(state: CliState, file: Optional[Path]) -> Path:
    """
    Decides which file to work on.

    :param state: The state the callback built.
    :param file: The value of --file, if one was given.
    :return: The configuration file to read.
    """
    if file is not None:
        return file
    return state.deployments_path / settings.config_filename


def validate(
    ctx: typer.Context,
    file: Optional[Path] = typer.Option(
        None,
        "--file",
        help="The configuration file to validate. Defaults to .opsmith/deployments.yml.",
    ),
) -> ValidateResult:
    """Validate the deployment configuration."""
    state: CliState = ctx.obj
    path = _config_path(state, file)

    result = validate_config_file(path)
    if not result.ok:
        count = len(result.errors)
        raise InvalidConfig(
            f"{path.name} has {count} error{'' if count == 1 else 's'}.",
            hint="Fix them and run 'opsmith config validate' again.",
            details={
                "path": str(path),
                "errors": [issue.model_dump() for issue in result.errors],
                "warnings": [issue.model_dump() for issue in result.warnings],
            },
        )

    events = state.context.events
    events.log(STEP_CONFIG, f"{path} is valid.")
    for warning in result.warnings:
        events.warning(STEP_CONFIG, f"{warning.path}: {warning.message}")

    return reported(state.context, result)


def schema(
    ctx: typer.Context,
    output_format: SchemaFormat = typer.Option(
        SchemaFormat.JSON,
        "--format",
        help="Whether to emit the JSON Schema itself or a markdown rendering of it.",
    ),
) -> ConfigSchemaResult:
    """Print the schema of the deployment configuration."""
    state: CliState = ctx.obj
    document = config_json_schema()

    if output_format is SchemaFormat.MARKDOWN:
        markdown = schema_to_markdown(document)
        state.renderer.render_document(markdown)
        return reported(
            state.context, ConfigSchemaResult(format=output_format.value, markdown=markdown)
        )

    state.renderer.render_document(json.dumps(document, indent=2))
    return reported(
        state.context, ConfigSchemaResult(format=output_format.value, schema_document=document)
    )


def show(
    ctx: typer.Context,
    file: Optional[Path] = typer.Option(
        None,
        "--file",
        help="The configuration file to show. Defaults to .opsmith/deployments.yml.",
    ),
) -> ConfigShowResult:
    """Print the deployment configuration, as Opsmith reads it."""
    state: CliState = ctx.obj
    config = load_config_file(_config_path(state, file))
    document = config.model_dump(mode="json")

    state.renderer.render_document(yaml.dump(document, indent=2, sort_keys=False))
    return reported(state.context, ConfigShowResult(config=document))
