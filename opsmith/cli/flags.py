"""Flags, and the answers they are shorthand for.

Every option a subcommand takes is sugar for an answer to a question the core asks:
``--region us-east-1`` is ``--answer env.region=us-east-1``, and ``--domain api=api.example.com``
is ``--answer env.domain.api=api.example.com``. That is the whole design. A subcommand collects
its options, this turns them into answers, and the interaction the run was going to build anyway
resolves them like any other - so there is exactly one path through a question, whether the
answer arrived as a flag, a file, an environment variable or a person typing it.

:data:`FLAG_KEYS` is the table. It is checked against the flag-shortcut column of the key table in
``docs/notes/2026-09-04-migration-plan.md`` by ``opsmith/tests/test_flag_mapping.py``, so a flag
cannot quietly come to mean something the plan does not say it means.
"""

from dataclasses import replace
from typing import Dict, List, Optional

from opsmith.cli.interaction import TerminalInteraction
from opsmith.cli.output import BaseRenderer
from opsmith.cli.state import CliState
from opsmith.core.answers import AnswerSources, AnswerStore
from opsmith.core.errors import InvalidArgument
from opsmith.core.interaction import HeadlessInteraction, Interaction

#: What each flag answers. A value holding ``<...>`` is a shape rather than a key: the flag
#: carries the missing part, and the parser below fills it in.
FLAG_KEYS: Dict[str, str] = {
    "--app-name": "app.name",
    "--rescan": "setup.action",
    "--name": "env.name",
    "--provider": "env.cloud_provider",
    "--strategy": "env.strategy",
    "--region": "env.region",
    "--project-id": "env.project_id",
    "--zone": "env.zone",
    "--instance-type": "env.instance_type",
    "--domain-email": "env.domain_email",
    "--domain": "env.domain.<slug>",
    "--env-var": "envvar.<KEY>",
    "--build-env": "build_env.<slug>.<KEY>",
}

#: What ``--rescan`` answers when it is given. It is a switch rather than a value, so the value
#: lives here instead of on the command line.
RESCAN_ANSWER = "rescan"


def _split_pair(flag: str, raw: str, separator: str, example: str) -> tuple:
    """
    Splits one of the flags that carries a name and a value together.

    :param flag: The flag being read, for the error.
    :param raw: What the user passed.
    :param separator: What divides the two halves.
    :param example: A well-formed value, for the hint.
    :return: The two halves, stripped.
    :raises InvalidArgument: The value does not hold the separator, or either half is empty.
    """
    name, found, value = raw.partition(separator)
    if not found or not name.strip():
        raise InvalidArgument(
            f"{flag} must be given as {example}, not '{raw}'.",
            hint=f"For example: {flag} {example}",
            details={"flag": flag, "value": raw},
        )
    return name.strip(), value


def parse_domains(values: List[str]) -> Dict[str, str]:
    """
    Reads ``--domain slug=host``.

    :param values: The raw strings the option collected.
    :return: The answers, by interaction key.
    :raises InvalidArgument: One of them is not a ``slug=host`` pair.
    """
    answers = {}
    for raw in values:
        slug, host = _split_pair("--domain", raw, "=", "api=api.example.com")
        answers[f"env.domain.{slug}"] = host
    return answers


def parse_env_vars(values: List[str]) -> Dict[str, str]:
    """
    Reads ``--env-var KEY=VALUE``.

    :param values: The raw strings the option collected.
    :return: The answers, by interaction key.
    :raises InvalidArgument: One of them is not a ``KEY=VALUE`` pair.
    """
    answers = {}
    for raw in values:
        key, value = _split_pair("--env-var", raw, "=", "DATABASE_URL=postgres://...")
        answers[f"envvar.{key}"] = value
    return answers


def parse_build_env_vars(values: List[str]) -> Dict[str, str]:
    """
    Reads ``--build-env slug:KEY=VALUE``, the build-time value of one service.

    :param values: The raw strings the option collected.
    :return: The answers, by interaction key.
    :raises InvalidArgument: One of them is not a ``slug:KEY=VALUE`` triple.
    """
    answers = {}
    for raw in values:
        slug, rest = _split_pair(
            "--build-env", raw, ":", "web:VITE_API_URL=https://api.example.com"
        )
        key, value = _split_pair(
            "--build-env", rest, "=", "web:VITE_API_URL=https://api.example.com"
        )
        answers[f"build_env.{slug}.{key}"] = value
    return answers


def answers_from_flags(
    scalars: Optional[Dict[str, Optional[str]]] = None,
    *,
    rescan: bool = False,
    domains: Optional[List[str]] = None,
    env_vars: Optional[List[str]] = None,
    build_envs: Optional[List[str]] = None,
) -> Dict[str, str]:
    """
    Turns what a subcommand was given into the answers it stands for.

    Pure, so the mapping can be tested without building a run.

    :param scalars: The one-value flags, keyed by the flag itself - ``{"--region": "us-east-1"}``.
        Keyed by flag rather than by interaction key so that the call site names the flag it is
        passing, and the table above stays the only place the two are connected.
    :param rescan: Whether ``--rescan`` was given.
    :param domains: The raw ``--domain slug=host`` strings.
    :param env_vars: The raw ``--env-var KEY=VALUE`` strings.
    :param build_envs: The raw ``--build-env slug:KEY=VALUE`` strings.
    :return: The answers, by interaction key.
    :raises InvalidArgument: A flag holds something it cannot be read as, or is not in the table.
    """
    answers: Dict[str, str] = {}

    for flag, value in (scalars or {}).items():
        if value is None:
            continue
        key = FLAG_KEYS.get(flag)
        if key is None or "<" in key:
            raise InvalidArgument(
                f"'{flag}' is not a flag that answers a single question.",
                hint="This is a bug in opsmith: every scalar flag belongs in flags.FLAG_KEYS.",
                details={"flag": flag},
            )
        answers[key] = value

    if rescan:
        answers[FLAG_KEYS["--rescan"]] = RESCAN_ANSWER

    answers.update(parse_domains(domains or []))
    answers.update(parse_env_vars(env_vars or []))
    answers.update(parse_build_env_vars(build_envs or []))
    return answers


def build_interaction(
    renderer: BaseRenderer,
    sources: AnswerSources,
    answers: AnswerStore,
    *,
    headless: bool,
    wait_timeout: int,
    resume: str,
) -> Interaction:
    """
    Builds the way this run reaches a person, or stands in for one.

    :param renderer: The run's renderer, which is also its event sink.
    :param sources: What the run was told up front.
    :param answers: What this environment has already answered.
    :param headless: Whether there is anybody to ask.
    :param wait_timeout: Seconds to poll an external action before giving up.
    :param resume: The command to run again, reported with every stop.
    :return: The interaction to put on the context.
    """
    if headless:
        return HeadlessInteraction(
            sources,
            answers,
            events=renderer,
            wait_timeout=wait_timeout,
            resume=resume,
        )
    return TerminalInteraction(renderer, sources=sources, answers=answers)


def supply(state: CliState, *, accept_reviews: bool = False, **flags) -> None:
    """
    Folds a subcommand's flags into the answers this run already had.

    The flags join the inline answers rather than replacing them, and lose to an explicit
    ``--answer`` for the same key, because ``--answer`` is documented as winning over every other
    source and a typed flag is just a friendlier way of writing one.

    Rebuilding the interaction is what makes the new answers visible: the sources are frozen, and
    the interaction reads them once.

    :param state: The run's state, holding the renderer, the store and the options.
    :param accept_reviews: Whether a review editor should take its proposal without opening. It is
        a switch rather than one of the flags below, because it answers no question: the proposal
        it accepts is the one the run had either way.
    :param flags: Whatever :func:`answers_from_flags` accepts.
    :raises InvalidArgument: A flag holds something it cannot be read as.
    """
    supplied = answers_from_flags(**flags)
    if not supplied and not accept_reviews:
        return

    sources = state.sources if state.sources is not None else AnswerSources()
    state.sources = replace(
        sources,
        inline={**supplied, **sources.inline},
        accept_reviews=sources.accept_reviews or accept_reviews,
    )
    state.context.interact = build_interaction(
        state.renderer,
        state.sources,
        state.context.answers,
        headless=state.headless,
        wait_timeout=state.wait_timeout,
        resume=state.resume,
    )
