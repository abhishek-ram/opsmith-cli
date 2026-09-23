"""Parsing and validating the deployment configuration.

The rules used to live inside the two inquirer callbacks in ``opsmith/cli/commands/setup.py``,
where nothing but a person at a terminal could reach them. They live here now, returning issues
instead of printing them, so the editors and ``opsmith config validate`` apply exactly the same
rules and a harness can check a repository without deploying anything.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from opsmith.core.errors import InvalidConfig
from opsmith.core.results import ConfigIssue, ValidateResult
from opsmith.types import (
    COMPATIBLE_PROVIDERS,
    DeploymentConfig,
    InfrastructureDependency,
    InfrastructureProviderEnum,
    ServiceInfo,
)

#: Re-exported so the editors in ``opsmith setup`` and the ``config`` commands keep importing the
#: issue and the result from the module whose rules produce them. They live in ``core/results.py``
#: with every other typed result.
__all__ = [
    "ConfigIssue",
    "ValidateResult",
    "check_infra_deps",
    "config_json_schema",
    "load_config_file",
    "parse_infra_deps",
    "parse_service",
    "markdown_section",
    "schema_to_markdown",
    "validate_config_data",
    "type_name",
    "validate_config_file",
    "warn_incompatible_providers",
]


def _dotted_path(location: Tuple[Any, ...], prefix: str = "") -> str:
    """
    Turns a pydantic error location into a dotted path.

    :param location: The ``loc`` tuple from a pydantic error.
    :param prefix: A path to put in front of it, for a fragment validated on its own.
    :return: A path such as "services.0.service_port".
    """
    parts = [str(part) for part in location]
    if prefix:
        parts.insert(0, prefix)
    return ".".join(parts) if parts else prefix


def _issues_from_validation_error(error: ValidationError, prefix: str = "") -> List[ConfigIssue]:
    """
    Turns a pydantic validation error into one issue per problem it found.

    :param error: The error pydantic raised.
    :param prefix: A path to put in front of each location.
    :return: One issue per underlying problem.
    """
    return [
        ConfigIssue(path=_dotted_path(item["loc"], prefix), message=item["msg"])
        for item in error.errors()
    ]


def _parse_yaml(text: str, prefix: str = "") -> Tuple[Any, List[ConfigIssue]]:
    """
    Parses YAML, reporting a syntax error as an issue rather than raising.

    :param text: The YAML to parse.
    :param prefix: The path to report a syntax error against.
    :return: The parsed data and the issues found, one of which is always empty.
    """
    try:
        return yaml.safe_load(text), []
    except yaml.YAMLError as err:
        return None, [ConfigIssue(path=prefix, message=f"Invalid YAML: {err}")]


def check_infra_deps(deps: List[InfrastructureDependency], prefix: str = "") -> List[ConfigIssue]:
    """
    Applies the rules that pydantic cannot express to a list of infrastructure dependencies.

    A provider left as ``user_choice`` is the model saying it could not decide, so it has to be
    replaced before anything can be deployed. Two entries with the same provider would render two
    services onto one name.

    :param deps: The dependencies to check.
    :param prefix: The path they live at in the document being validated.
    :return: One issue per problem found.
    """
    issues: List[ConfigIssue] = []
    seen_providers = set()

    for index, dep in enumerate(deps):
        path = _dotted_path((index, "provider"), prefix)

        if dep.provider is InfrastructureProviderEnum.USER_CHOICE:
            issues.append(
                ConfigIssue(
                    path=path,
                    message="Provider is 'user_choice'. Please replace it with a valid provider.",
                )
            )
        elif dep.provider in seen_providers:
            issues.append(
                ConfigIssue(
                    path=path,
                    message=(
                        f"Duplicate provider found: {dep.provider.value}. Each provider can"
                        " only be listed once."
                    ),
                )
            )
        seen_providers.add(dep.provider)

    return issues


def warn_incompatible_providers(
    deps: List[InfrastructureDependency], prefix: str = ""
) -> List[ConfigIssue]:
    """
    Reports dependencies whose provider is not one of those known for its type.

    This is a warning rather than an error: the pairing is unusual rather than impossible, and
    ``COMPATIBLE_PROVIDERS`` is advisory.

    :param deps: The dependencies to check.
    :param prefix: The path they live at in the document being validated.
    :return: One issue per unusual pairing.
    """
    warnings: List[ConfigIssue] = []
    for index, dep in enumerate(deps):
        if dep.provider is InfrastructureProviderEnum.USER_CHOICE:
            # Already an error; saying it is also an unusual pairing is noise.
            continue

        compatible = COMPATIBLE_PROVIDERS.get(dep.dependency_type, [])
        if compatible and dep.provider not in compatible:
            names = ", ".join(provider.value for provider in compatible)
            warnings.append(
                ConfigIssue(
                    path=_dotted_path((index, "provider"), prefix),
                    message=(
                        f"{dep.provider.value} is not a usual provider for"
                        f" {dep.dependency_type.value}. Expected one of: {names}."
                    ),
                )
            )
    return warnings


def parse_service(text: str) -> Tuple[Optional[ServiceInfo], List[ConfigIssue]]:
    """
    Parses one service from the YAML an editor produced.

    :param text: The YAML the user edited.
    :return: The service, or None with the issues that stopped it parsing.
    """
    data, issues = _parse_yaml(text)
    if issues:
        return None, issues

    if not isinstance(data, dict):
        return None, [ConfigIssue(path="", message="Configuration must be a YAML mapping.")]

    try:
        return ServiceInfo(**data), []
    except ValidationError as err:
        return None, _issues_from_validation_error(err)


def parse_infra_deps(
    text: str,
) -> Tuple[Optional[List[InfrastructureDependency]], List[ConfigIssue]]:
    """
    Parses the infrastructure dependency list from the YAML an editor produced.

    :param text: The YAML the user edited.
    :return: The dependencies, or None with the issues that stopped them parsing.
    """
    data, issues = _parse_yaml(text)
    if issues:
        return None, issues

    if not isinstance(data, list):
        return None, [
            ConfigIssue(path="", message="Configuration must be a YAML list of dependencies.")
        ]

    try:
        deps = [InfrastructureDependency(**item) for item in data]
    except (ValidationError, TypeError) as err:
        if isinstance(err, ValidationError):
            return None, _issues_from_validation_error(err)
        return None, [ConfigIssue(path="", message=f"Invalid dependency: {err}")]

    rule_issues = check_infra_deps(deps)
    if rule_issues:
        return None, rule_issues

    return deps, []


def validate_config_data(data: Any) -> ValidateResult:
    """
    Validates an already parsed deployment configuration document.

    :param data: What the YAML parsed to.
    :return: The errors and warnings found.
    """
    if not isinstance(data, dict):
        return ValidateResult(
            ok=False,
            errors=[ConfigIssue(path="", message="Configuration must be a YAML mapping.")],
        )

    try:
        config = DeploymentConfig(**data)
    except ValidationError as err:
        return ValidateResult(ok=False, errors=_issues_from_validation_error(err))

    errors = check_infra_deps(config.infra_deps, prefix="infra_deps")
    warnings = warn_incompatible_providers(config.infra_deps, prefix="infra_deps")
    return ValidateResult(ok=not errors, errors=errors, warnings=warnings)


def validate_config_file(path: Path) -> ValidateResult:
    """
    Validates a deployment configuration file.

    :param path: The file to validate.
    :return: The errors and warnings found.
    :raises InvalidConfig: There is no file at that path.
    """
    if not path.exists():
        raise InvalidConfig(
            f"No deployment configuration found at {path}.",
            hint="Run 'opsmith setup' to create one, or pass --file to point at another.",
            details={"path": str(path)},
        )

    data, issues = _parse_yaml(path.read_text())
    if issues:
        return ValidateResult(ok=False, errors=issues)

    if data is None:
        return ValidateResult(
            ok=False, errors=[ConfigIssue(path="", message="Configuration file is empty.")]
        )

    return validate_config_data(data)


def load_config_file(path: Path) -> DeploymentConfig:
    """
    Loads a deployment configuration file, refusing anything that does not validate.

    :param path: The file to load.
    :return: The parsed configuration.
    :raises InvalidConfig: The file is missing, unparsable or invalid.
    """
    result = validate_config_file(path)
    if not result.ok:
        raise InvalidConfig(
            f"{path.name} is not valid.",
            hint="Run 'opsmith config validate' to see every problem.",
            details={"path": str(path), "errors": [issue.model_dump() for issue in result.errors]},
        )

    return DeploymentConfig(**yaml.safe_load(path.read_text()))


def config_json_schema() -> Dict:
    """
    Returns the JSON Schema of the deployment configuration.

    :return: The schema pydantic generates from ``DeploymentConfig``.
    """
    return DeploymentConfig.model_json_schema()


def type_name(schema: Dict) -> str:
    """
    Names the type of one property, as far as a table cell can.

    :param schema: The property's schema fragment.
    :return: A short type name such as "string", "ServiceInfo[]" or "string | null".
    """
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]

    if "anyOf" in schema:
        return " | ".join(type_name(option) for option in schema["anyOf"])

    kind = schema.get("type")
    if kind == "array":
        return f"{type_name(schema.get('items', {}))}[]"
    if kind is None and "enum" in schema:
        return " | ".join(str(value) for value in schema["enum"])
    return str(kind) if kind is not None else "any"


def markdown_section(name: str, definition: Dict) -> List[str]:
    """
    Renders one model of the schema as a markdown section.

    :param name: The model's name.
    :param definition: Its schema fragment.
    :return: The lines of the section.
    """
    lines = [f"## {name}", ""]
    if definition.get("description"):
        lines += [definition["description"], ""]

    if definition.get("enum"):
        lines += ["One of: " + ", ".join(f"`{value}`" for value in definition["enum"]), ""]
        return lines

    properties = definition.get("properties", {})
    if not properties:
        return lines

    required = set(definition.get("required", []))
    lines += ["| Field | Type | Required | Default | Description |", "|---|---|---|---|---|"]
    for field_name, field_schema in properties.items():
        default = field_schema.get("default", "")
        lines.append(
            f"| `{field_name}` | {type_name(field_schema)} |"
            f" {'yes' if field_name in required else 'no'} |"
            f" {f'`{default}`' if default != '' else ''} |"
            f" {field_schema.get('description', '')} |"
        )
    lines.append("")
    return lines


def schema_to_markdown(schema: Dict) -> str:
    """
    Renders a JSON Schema as markdown, one section per model.

    :param schema: The schema to render.
    :return: The markdown document.
    """
    lines = [f"# {schema.get('title', 'DeploymentConfig')}", ""]
    lines += markdown_section(schema.get("title", "DeploymentConfig"), schema)[2:]

    for name, definition in sorted(schema.get("$defs", {}).items()):
        lines += markdown_section(name, definition)

    return "\n".join(lines).rstrip() + "\n"
