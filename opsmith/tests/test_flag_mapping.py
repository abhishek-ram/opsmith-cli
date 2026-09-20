"""Every flag is sugar for an answer, and the plan says which answer.

A typed flag such as ``--region`` and the generic ``--answer env.region=...`` have to mean the
same thing, or a harness reading the documentation and a person reading ``--help`` are driving two
different tools. The pairing is declared in the flag-shortcut column of the key table in the
migration plan, and read back out of it here, for the same reason
``test_interaction_keys.py`` reads the key column: one table, checked, rather than two lists that
agree until somebody edits one of them.
"""

import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

from opsmith.cli.flags import (
    FLAG_KEYS,
    RESCAN_ANSWER,
    answers_from_flags,
    parse_build_env_vars,
    parse_domains,
    parse_env_vars,
)
from opsmith.core.errors import InvalidArgument

KEY_TABLE = (
    Path(__file__).resolve().parent.parent.parent
    / "docs"
    / "notes"
    / "2026-09-04-migration-plan.md"
)

#: The header that opens the table this reads. The document holds several tables of three columns;
#: only this one pairs a key with a flag.
TABLE_HEADER = "| Key | Asked by | Flag shortcut |"

#: What a placeholder is reduced to before comparing, so ``env.domain.<slug>`` in the plan and
#: ``env.domain.<slug>`` in the code would still match had either chosen a different word.
PLACEHOLDER = "<*>"

#: Flags that change how a question is answered rather than naming which question. They appear in
#: the shortcut column of several rows because they are how a default or a proposal is taken, and
#: they are not part of the flag-to-key mapping.
GENERIC_FLAGS = {"--answer", "--accept-defaults", "--accept-detected", "--wait-timeout"}

#: Flags the plan declares for a phase that has not been built. They must be in the table and
#: must not be in the code; asserting both is what stops this test passing because a 0f flag
#: quietly disappeared alongside them.
LATER_PHASES = {
    "--workload-tier",  # phase 2
    "--workload",  # phase 2
    "--state",  # phase 4
    "--input",  # phase 5
}

#: The one row whose keys and flags cannot be paired off, because it declares three keys and two
#: flags plus a note that the third is answered generically. It is a phase 2 row, so nothing here
#: depends on reading it.
UNPAIRABLE_ROW_KEYS = {
    "env.workload.tier",
    "env.workload.description",
    "env.workload.<*>",
}

#: Pairs that must be found, so a parser that matched nothing could not make this pass.
EXPECTED_PAIRS = {
    ("--name", "env.name"),
    ("--region", "env.region"),
    ("--domain", "env.domain.<*>"),
    ("--env-var", "envvar.<*>"),
}


def _normalise(text: str) -> str:
    """
    :param text: A key as either the plan or the code writes it.
    :return: The key with every placeholder reduced to one shape.
    """
    return re.sub(r"<[^>]+>", PLACEHOLDER, text)


def _table_rows() -> List[Tuple[List[str], List[str]]]:
    """
    Reads the key table out of the migration plan.

    :return: The keys and the flags of each row, in the order the table lists them.
    :raises AssertionError: The table is not where this expects it, which means the plan was
        restructured and this test is reading nothing.
    """
    lines = KEY_TABLE.read_text(encoding="utf-8").splitlines()
    assert TABLE_HEADER in lines, f"the key table is no longer headed '{TABLE_HEADER}'"

    rows = []
    for line in lines[lines.index(TABLE_HEADER) + 2 :]:
        if not line.startswith("|"):
            break

        # An escaped pipe inside a cell is content, not a column boundary.
        columns = line.replace(r"\|", "\0").split("|")
        keys = [_normalise(key) for key in re.findall(r"`([^`]+)`", columns[1])]
        flags = [
            # A cell reads `--domain slug=host`; the flag is the first word of it.
            token.replace("\0", "|").split()[0]
            for token in re.findall(r"`([^`]+)`", columns[3])
        ]
        rows.append((keys, [flag for flag in flags if flag not in GENERIC_FLAGS]))
    return rows


def _declared_pairs() -> Dict[str, str]:
    """
    Pairs each flag the plan declares with the key it answers.

    A row names one key and one flag, or several of each in the same order. A row that names no
    flag declares nothing here - the question is answered some other way, or not from the command
    line at all.

    :return: The key each flag answers, by flag.
    :raises AssertionError: A row names keys and flags in numbers that cannot be paired and is not
        the one row known to do so.
    """
    pairs: Dict[str, str] = {}
    for keys, flags in _table_rows():
        if not flags:
            continue
        if len(keys) != len(flags):
            assert set(keys) == UNPAIRABLE_ROW_KEYS, f"cannot pair {keys} with {flags}"
            continue
        pairs.update(zip(flags, keys))
    return pairs


def test_every_flag_answers_the_key_the_plan_gives_it():
    """
    Walks the plan's table and asserts the code agrees about what each flag means, and that the
    flags belonging to later phases have not been implemented early and quietly.
    """
    in_code = {flag: _normalise(key) for flag, key in FLAG_KEYS.items()}

    for flag, key in _declared_pairs().items():
        if flag in LATER_PHASES:
            assert flag not in in_code, f"{flag} belongs to a later phase but is implemented"
            continue
        assert in_code.get(flag) == key, f"{flag} should answer {key}, not {in_code.get(flag)}"


def test_every_flag_in_the_code_is_one_the_plan_declares():
    """
    The other direction: a flag that answers a question has to be in the table, so a new shortcut
    cannot ship without the plan saying what it is shorthand for.
    """
    declared = _declared_pairs()
    undeclared = sorted(flag for flag in FLAG_KEYS if flag not in declared)

    assert not undeclared, (
        "Every flag shortcut must appear in the key table in"
        f" docs/notes/2026-09-04-migration-plan.md: {undeclared}"
    )


def test_the_table_is_actually_being_read():
    """
    Guards the parser: one that matched nothing would make both tests above pass vacuously, so a
    handful of pairs that certainly are in the table have to turn up.
    """
    found: Set[Tuple[str, str]] = set(_declared_pairs().items())

    assert EXPECTED_PAIRS <= found, f"missing: {sorted(EXPECTED_PAIRS - found)}"

    # Read from the rows rather than from the pairs, because two of these are on the one row the
    # pairing deliberately skips.
    in_the_table = {flag for _, flags in _table_rows() for flag in flags}
    assert LATER_PHASES <= in_the_table, f"left the table: {sorted(LATER_PHASES - in_the_table)}"


def test_the_pattern_flags_build_the_keys_the_plan_declares():
    """
    The three flags that carry a name as well as a value build their key from what they carry, so
    the shape in the table is what has to come out of them.
    """
    assert parse_domains(["api=api.example.com"]) == {"env.domain.api": "api.example.com"}
    assert parse_env_vars(["DATABASE_URL=postgres://x"]) == {"envvar.DATABASE_URL": "postgres://x"}
    assert parse_build_env_vars(["web:VITE_API=https://api.example.com"]) == {
        "build_env.web.VITE_API": "https://api.example.com"
    }

    declared = {_normalise(key) for key in _declared_pairs().values()}
    assert "env.domain.<*>" in declared
    assert "envvar.<*>" in declared
    assert "build_env.<*>.<*>" in declared


def test_a_value_a_pattern_flag_cannot_read_is_a_usage_error():
    """
    A malformed flag is reported before anything is asked, as INVALID_ARGUMENT rather than as a
    question nothing answered - the driver supplied something, it just cannot be read.
    """
    with pytest.raises(InvalidArgument) as raised:
        parse_domains(["api.example.com"])
    assert raised.value.details["flag"] == "--domain"

    with pytest.raises(InvalidArgument):
        parse_build_env_vars(["web=VITE_API=x"])


def test_flags_become_the_answers_they_stand_for():
    """
    The whole point, end to end: a subcommand's options come out as a mapping of interaction key
    to answer, which is exactly what --answer would have supplied.
    """
    answers = answers_from_flags(
        {"--name": "dev", "--region": "us-east-1", "--instance-type": None},
        rescan=True,
        domains=["api=api.example.com"],
        env_vars=["SECRET_KEY=hunter2"],
        build_envs=["web:VITE_API=https://api.example.com"],
    )

    assert answers == {
        "env.name": "dev",
        "env.region": "us-east-1",
        "setup.action": RESCAN_ANSWER,
        "env.domain.api": "api.example.com",
        "envvar.SECRET_KEY": "hunter2",
        "build_env.web.VITE_API": "https://api.example.com",
    }
