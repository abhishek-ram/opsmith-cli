"""Generating the parts of the Agent Skill that must never drift from the code.

The skill is mostly prose, written by hand. Two of its references are not: what the commands are
and what the configuration schema is, both of which change whenever the code does. They are
generated here from the Typer app and the pydantic models themselves, committed alongside the
prose, and compared against the generator by ``opsmith/tests/test_skill_refs.py``. A command, flag,
exit code or schema field that changed without a regenerate fails CI.

This lives under ``opsmith/cli/`` because it walks the Typer app, and nothing outside ``cli/`` may
import typer. It takes the app as an argument rather than importing it, so that a command module
may import this one without closing a cycle.

Adding a reference is adding one entry to :data:`GENERATED_REFERENCES`; the test iterates it, so a
later phase's reference inherits the freshness check without editing the test.
"""

import inspect
import re
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Type,
    get_type_hints,
)

from pydantic import BaseModel

from opsmith.cli.commands import needs_model, requirements_of
from opsmith.core import errors
from opsmith.core.config import config_json_schema, markdown_section, schema_to_markdown
from opsmith.core.errors import EXIT_CODES, OpsmithError

#: Where the generated references are committed.
SKILL_REFERENCES_DIR = Path(__file__).parent.parent / "skill" / "references"

#: Parameters Typer adds to the root group that are about the shell rather than about Opsmith.
SHELL_PARAMS = {"install_completion", "show_completion"}


def _generated_header(source: str) -> List[str]:
    """
    :param source: What this file is generated from, in a few words.
    :return: The banner every generated reference opens with.
    """
    return [
        f"<!-- Generated from {source} by scripts/build_skill_refs.py. Do not edit. -->",
        "",
    ]


# --- walking the app ----------------------------------------------------------------------------


def _children(node: Any) -> Optional[Dict[str, Any]]:
    """
    Returns the subcommands of a node, duck-typed.

    Typer vendors its own click, so the group it builds is not an instance of the installed
    ``click.Group`` and an ``isinstance`` check here would silently skip every sub-app.

    :param node: A command or a group.
    :return: Its subcommands, or None when it is a leaf.
    """
    children = getattr(node, "commands", None)
    return children if children else None


def walk_commands(node: Any, prefix: str = "") -> Iterator[Tuple[str, Any]]:
    """
    Walks every leaf command of the app, in a stable order.

    :param node: The group to walk.
    :param prefix: The command path built so far.
    :return: Each command's full path and the command itself.
    """
    children = _children(node)
    if children is None:
        return

    for name in sorted(children):
        child = children[name]
        path = f"{prefix} {name}".strip()
        if _children(child) is not None:
            yield from walk_commands(child, path)
        else:
            yield path, child


def walk_groups(node: Any, prefix: str = "") -> Iterator[Tuple[str, Any]]:
    """
    :param node: The group to walk.
    :param prefix: The command path built so far.
    :return: Each sub-group's full path and the group itself.
    """
    children = _children(node)
    if children is None:
        return

    for name in sorted(children):
        child = children[name]
        if _children(child) is None:
            continue
        path = f"{prefix} {name}".strip()
        yield path, child
        yield from walk_groups(child, path)


def _callback_of(command: Any) -> Optional[Callable]:
    """
    :param command: A click command.
    :return: The function the command was built from, unwrapped past ``handle_errors``.
    """
    callback = getattr(command, "callback", None)
    if callback is None:
        return None
    return getattr(callback, "__wrapped__", callback)


def _help_of(node: Any) -> str:
    """
    :param node: A command or a group.
    :return: Its help text, cleaned of the indentation a docstring carries.
    """
    return inspect.cleandoc(getattr(node, "help", None) or "").strip()


# --- rendering one command ------------------------------------------------------------------


def _default_text(param: Any) -> str:
    """
    Describes a parameter's default without letting a repr into a generated file.

    An ``Enum``'s repr has shifted between Python versions, and this file is generated on three of
    them, so every default is rendered from its value rather than from how it prints.

    :param param: A click parameter.
    :return: The default, as a table cell.
    """
    if getattr(param, "required", False):
        return "required"

    default = getattr(param, "default", None)
    if default is None or default == () or default == []:
        return "—"
    if isinstance(default, Enum):
        return f"`{default.value}`"
    if isinstance(default, bool):
        return f"`{'true' if default else 'false'}`"
    return f"`{default}`"


def _type_text(param: Any) -> str:
    """
    :param param: A click parameter.
    :return: What it accepts, named the way click names it.
    """
    param_type = getattr(param, "type", None)
    choices = getattr(param_type, "choices", None)
    if choices:
        return " \\| ".join(f"`{choice}`" for choice in choices)

    name = getattr(param_type, "name", "text")
    if getattr(param, "multiple", False):
        return f"{name}, repeatable"
    return str(name)


def _param_rows(params: List[Any]) -> List[str]:
    """
    :param params: The parameters of one command, in declaration order.
    :return: The table describing them, or nothing when there are none.
    """
    rows = []
    for param in params:
        if param.name in SHELL_PARAMS or param.name == "help":
            continue

        opts = getattr(param, "opts", []) or []
        if not opts:
            continue

        flags = ", ".join(f"`{opt}`" for opt in opts)
        if not opts[0].startswith("-"):
            flags = f"`{param.name}` (argument)"

        help_text = (getattr(param, "help", "") or "").replace("\n", " ")
        rows.append(f"| {flags} | {_type_text(param)} | {_default_text(param)} | {help_text} |")

    if not rows:
        return []
    return ["| Flag | Type | Default | Description |", "|---|---|---|---|"] + rows + [""]


def _result_model(callback: Optional[Callable]) -> Tuple[str, List[Type[BaseModel]]]:
    """
    Works out what a command hands back.

    :param callback: The command function.
    :return: How to describe its result, and the result models it names.
    """
    if callback is None:
        return "nothing", []

    hints = get_type_hints(callback)
    annotation = hints.get("return")
    if annotation is None:
        return "nothing", []

    models = _models_in(annotation)
    if not models:
        return "an untyped JSON object", []
    if len(models) == 1:
        return f"[`{models[0].__name__}`](#{models[0].__name__.lower()})", models
    names = ", ".join(f"[`{model.__name__}`](#{model.__name__.lower()})" for model in models)
    return f"one of: {names}", models


def _models_in(annotation: Any) -> List[Type[BaseModel]]:
    """
    :param annotation: A return annotation.
    :return: The pydantic models it mentions, in the order it mentions them.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]

    found: List[Type[BaseModel]] = []
    for argument in getattr(annotation, "__args__", ()) or ():
        found += _models_in(argument)
    return found


def _command_section(path: str, command: Any) -> Tuple[List[str], List[Type[BaseModel]]]:
    """
    Renders one command.

    :param path: The full command path, such as "env create".
    :param command: The click command.
    :return: The lines describing it, and the result models it names.
    """
    callback = _callback_of(command)
    tools = requirements_of(callback) if callback is not None else ()
    result_text, models = _result_model(callback)

    lines = [f"### opsmith {path}", ""]
    help_text = _help_of(command)
    if help_text:
        lines += [help_text, ""]

    if tools:
        needs = f"**Needs:** {', '.join(f'`{tool}`' for tool in sorted(tools))}."
    else:
        needs = (
            "**Needs:** nothing - it runs on a machine with neither docker nor terraform installed."
        )
    if callback is not None and not needs_model(callback):
        needs += " It does not need a model configured either."
    lines += [needs, "", f"**Result:** {result_text}.", ""]

    if any(
        model.__name__ == "RunResult" or hasattr(model, "process_exit_code") for model in models
    ):
        lines += [
            (
                "**This command exits with the status of the command it ran**, after writing a"
                " successful envelope. Read `ok` in the envelope before reading the exit code."
            ),
            "",
        ]

    lines += _param_rows(list(getattr(command, "params", []) or []))
    return lines, models


# --- the references -------------------------------------------------------------------------


def render_commands_markdown(app: Any) -> str:
    """
    Renders every command of the CLI, with what it needs and what it returns.

    :param app: The Typer application.
    :return: The reference, as markdown.
    """
    import typer.main

    root = typer.main.get_command(app)

    lines = _generated_header("the Typer application")
    lines += [
        "# Opsmith commands",
        "",
        (
            "Every command accepts the global options below. With `--output json` a command writes"
            " exactly one JSON document to stdout and sends all progress to stderr."
        ),
        "",
        (
            "**Every command needs a model configured** - `--model` or `OPSMITH_MODEL`, plus a key"
            " - except the few that say otherwise below. A model is part of the tool rather than"
            " an option, and a command that needs no external tool may still need one."
        ),
        "",
        "## Global options",
        "",
    ]
    lines += _param_rows(list(getattr(root, "params", []) or []))

    lines += ["## Command groups", "", "| Group | What it is for |", "|---|---|"]
    for path, group in walk_groups(root):
        lines.append(f"| `opsmith {path}` | {_help_of(group)} |")
    lines.append("")

    lines += ["## Commands", ""]
    models: List[Type[BaseModel]] = []
    for path, command in walk_commands(root):
        section, named = _command_section(path, command)
        lines += section
        models += named

    lines += _exit_code_section()
    lines += _error_code_section()
    lines += _result_shape_section(models)

    return _plain_backticks("\n".join(lines).rstrip() + "\n")


def _plain_backticks(text: str) -> str:
    """
    Turns the double backticks of a Python docstring into the single ones markdown reads.

    :param text: The rendered reference.
    :return: The same, with code spans a harness's markdown reader will render.
    """
    return re.sub(r"``([^`]+)``", r"`\1`", text)


def _exit_code_section() -> List[str]:
    """
    :return: The exit codes, from the map the CLI itself exits by.
    """
    by_code: Dict[int, List[str]] = {}
    for code, exit_code in EXIT_CODES.items():
        by_code.setdefault(exit_code, []).append(code)

    lines = [
        "## Exit codes",
        "",
        "| Exit code | Error codes |",
        "|---|---|",
        "| 0 | success |",
    ]
    for exit_code in sorted(by_code):
        codes = ", ".join(f"`{code}`" for code in sorted(by_code[exit_code]))
        lines.append(f"| {exit_code} | {codes} |")
    lines.append("")
    return lines


def _error_code_section() -> List[str]:
    """
    :return: Every error Opsmith raises, with what it means and what it exits with.
    """
    classes = [
        value
        for value in vars(errors).values()
        if isinstance(value, type) and issubclass(value, OpsmithError) and value is not OpsmithError
    ]

    rows = []
    for error_class in classes:
        code = getattr(error_class, "code", "")
        if not code:
            continue
        summary = inspect.cleandoc(error_class.__doc__ or "").splitlines()[0]
        rows.append((EXIT_CODES.get(code, 1), code, summary))

    lines = ["## Error codes", "", "| Code | Exit | Means |", "|---|---|---|"]
    for exit_code, code, summary in sorted(set(rows)):
        lines.append(f"| `{code}` | {exit_code} | {summary} |")
    lines.append("")
    return lines


def _result_shape_section(models: List[Type[BaseModel]]) -> List[str]:
    """
    :param models: Every result model named by a command.
    :return: The field table of each of them, and of everything they carry.
    """
    seen: Dict[str, Type[BaseModel]] = {model.__name__: model for model in models}
    shared: Dict[str, Dict] = {}

    lines = ["## Result shapes", ""]
    for name in sorted(seen):
        schema = seen[name].model_json_schema()
        lines += markdown_section(name, schema)
        for definition_name, definition in (schema.get("$defs") or {}).items():
            shared.setdefault(definition_name, definition)

    for name in sorted(shared):
        if name in seen:
            continue
        lines += markdown_section(name, shared[name])

    return lines


def render_config_schema_markdown(app: Any) -> str:
    """
    Renders the configuration schema, by the same call ``opsmith config schema`` makes.

    :param app: The Typer application, which this reference does not need.
    :return: The reference, as markdown.
    """
    return "\n".join(_generated_header("the pydantic models")) + schema_to_markdown(
        config_json_schema()
    )


#: Every reference generated from the code. A later phase adds one entry; the freshness test walks
#: this mapping, so nothing about the test has to change.
GENERATED_REFERENCES: Dict[str, Callable[[Any], str]] = {
    "commands.md": render_commands_markdown,
    "config-schema.md": render_config_schema_markdown,
}


def generate(app: Any) -> Dict[str, str]:
    """
    :param app: The Typer application.
    :return: What each generated reference should hold, by file name.
    """
    return {name: render(app) for name, render in GENERATED_REFERENCES.items()}


def write(app: Any, directory: Path = SKILL_REFERENCES_DIR) -> List[Path]:
    """
    Writes every generated reference.

    :param app: The Typer application.
    :param directory: Where the references live.
    :return: The files written.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in generate(app).items():
        path = directory / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def stale(app: Any, directory: Path = SKILL_REFERENCES_DIR) -> List[str]:
    """
    :param app: The Typer application.
    :param directory: Where the references live.
    :return: The names of the references that are missing or out of date.
    """
    out_of_date = []
    for name, content in generate(app).items():
        path = directory / name
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            out_of_date.append(name)
    return out_of_date
