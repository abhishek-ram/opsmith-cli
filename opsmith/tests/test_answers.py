"""The answer store: what a run is told, and what it remembers being told.

The property every test here defends is that an answer given once is never asked for again. That
is what makes a headless run resumable: it stops for the one thing nobody told it, and the next
invocation starts from everything that was already said.
"""

import os
import stat
from pathlib import Path

import pytest
import yaml

from opsmith.core.answers import (
    SOURCE_ANSWERS_FILE,
    SOURCE_ENV_FILE,
    SOURCE_ENVIRONMENT,
    SOURCE_INLINE,
    AnswerSources,
    AnswerStore,
    environment_variable_for,
    load_answers_file,
    load_env_file,
    parse_inline_answers,
)
from opsmith.core.errors import InvalidArgument
from opsmith.tests.conftest import RecordingSink


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """The project's state directory, which is outside the project."""
    return tmp_path / "opsmith-state"


@pytest.fixture
def store(state_dir: Path, events: RecordingSink) -> AnswerStore:
    """A store bound to one environment, as a run that named one would have."""
    store = AnswerStore(state_dir, events=events)
    store.use_environment("prod")
    return store


def _read(path: Path) -> dict:
    """
    :param path: A file the store wrote.
    :return: What it holds.
    """
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_an_answer_is_on_disk_before_anything_asks_it_to_be_saved(store: AnswerStore):
    """
    Recording writes through. A run that stops at the next question has already unwound past
    anywhere that could have flushed a buffer, so a buffer would lose exactly the answers the
    resume was supposed to keep.
    """
    store.record("env.region", "us-east-1")

    assert _read(store.answers_path) == {"env.region": "us-east-1"}


def test_the_store_writes_a_file_the_answers_option_can_read_back(store: AnswerStore):
    """
    What the store writes is a flat mapping of key to answer, which is exactly what `--answers`
    reads. So the file an environment accumulates can be handed to another run verbatim.
    """
    store.record("env.region", "us-east-1")
    store.record("env.domain_email", "ops@example.test")

    assert load_answers_file(store.answers_path) == {
        "env.region": "us-east-1",
        "env.domain_email": "ops@example.test",
    }


def test_a_secret_is_kept_out_of_the_file_that_is_committed(store: AnswerStore):
    """
    A secret goes to the cache beside the answers, never into the answers themselves, and the
    cache is readable only by the person who ran the command.
    """
    store.record("env.region", "us-east-1")
    store.record("envvar.DATABASE_URL", "postgres://user:pw@host/db", secret=True)

    assert _read(store.answers_path) == {"env.region": "us-east-1"}
    assert _read(store.secrets_path) == {"envvar.DATABASE_URL": "postgres://user:pw@host/db"}

    mode = stat.S_IMODE(os.stat(store.secrets_path).st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_nothing_remembered_is_written_into_the_project(tmp_path: Path, state_dir: Path):
    """
    None of this belongs in a repository, and the secret half must not be in one at any cost: an
    ignore rule is advice, and says nothing about `git add -f`, an archive of the directory, or a
    build context that never read it. Keeping it outside is the only promise that holds.
    """
    project = tmp_path / "project"
    (project / ".opsmith").mkdir(parents=True)

    store = AnswerStore(state_dir)
    store.use_environment("prod")
    store.record("env.region", "us-east-1")
    store.record("envvar.SECRET_KEY", "hunter2", secret=True)

    assert list(project.rglob("*.yml")) == []
    assert store.secrets_path.is_relative_to(state_dir)


def test_the_state_directory_is_readable_only_by_its_owner(store: AnswerStore):
    """It holds secrets, and a home directory is not private by default on every machine."""
    store.record("envvar.SECRET_KEY", "hunter2", secret=True)

    assert stat.S_IMODE(os.stat(store.root).st_mode) == stat.S_IRWXU


def test_a_secret_survives_a_re_run(state_dir: Path):
    """A second run over the same environment reads back what the first one was told."""
    first = AnswerStore(state_dir)
    first.use_environment("prod")
    first.record("envvar.SECRET_KEY", "hunter2", secret=True)
    first.record("env.region", "us-east-1")

    second = AnswerStore(state_dir)
    second.use_environment("prod")

    assert second.get("envvar.SECRET_KEY") == "hunter2"
    assert second.get("env.region") == "us-east-1"


def test_answers_given_before_the_environment_is_named_are_not_lost(state_dir: Path):
    """
    The question that names the environment is itself an answer, so a run answers things before
    it knows where to put them. They are held until it does.
    """
    store = AnswerStore(state_dir)
    store.record("env.cloud_provider", "AWS")
    store.record("envvar.SECRET_KEY", "hunter2", secret=True)

    assert not state_dir.exists()

    store.use_environment("prod")

    assert _read(store.answers_path) == {"env.cloud_provider": "AWS"}
    assert _read(store.secrets_path) == {"envvar.SECRET_KEY": "hunter2"}


def test_a_run_answers_for_one_environment_only(store: AnswerStore):
    """
    Binding twice would mean one run wrote answers into two environments' files, which is never
    what was meant and would be hard to notice.
    """
    with pytest.raises(InvalidArgument):
        store.use_environment("staging")


@pytest.mark.parametrize(
    "key", ["delete.confirm", "update.confirm_infra_changes", "env.action", "run.command"]
)
def test_what_describes_one_invocation_is_never_remembered(store: AnswerStore, key: str):
    """
    A menu choice and a destructive confirmation describe one run, not the environment. Persisting
    a typed deletion would mean the next run destroyed the environment without asking anybody.
    """
    store.record(key, "DELETE")

    assert store.get(key) is None
    assert not store.answers_path.exists() or _read(store.answers_path) in (None, {})


def test_the_deployed_machine_wins_over_the_local_cache(store: AnswerStore):
    """
    The cache exists to carry a secret until something is running with it. Once something is, the
    value it is actually running with is the answer.
    """
    store.record("envvar.DATABASE_URL", "postgres://local", secret=True)

    store.adopt_env_file('DATABASE_URL="postgres://the-machine"\n')

    assert store.get("envvar.DATABASE_URL") == "postgres://the-machine"


def test_adopting_the_machines_file_drops_only_what_that_file_covers(store: AnswerStore):
    """
    Build-time values never reach the machine's environment file - they are baked into the assets
    long before - so clearing the cache wholesale would lose them.
    """
    store.record("envvar.DATABASE_URL", "postgres://local", secret=True)
    store.record("build_env.web.API_TOKEN", "token", secret=True)

    store.adopt_env_file('DATABASE_URL="postgres://the-machine"\n')

    assert _read(store.secrets_path) == {"build_env.web.API_TOKEN": "token"}
    assert store.get("build_env.web.API_TOKEN") == "token"


def test_nothing_is_written_for_a_command_that_names_no_environment(state_dir: Path):
    """
    `setup` never names an environment, and everything it asks is already durable somewhere
    better - the application name becomes the configuration it writes. So an unbound store keeps
    its answers in memory and leaves nothing behind.
    """
    store = AnswerStore(state_dir)
    store.record("app.name", "Acme")

    assert store.get("app.name") == "Acme"
    assert not state_dir.exists()


# --- the sources a run is told things through -----------------------------------------------


def test_an_inline_answer_must_name_what_it_answers():
    """Without a key there is nothing to match an answer to, and silently ignoring it is worse."""
    with pytest.raises(InvalidArgument) as raised:
        parse_inline_answers(["env.region"])

    assert raised.value.details["answer"] == "env.region"


def test_an_inline_answer_keeps_every_equals_sign_after_the_first():
    """A connection string is a perfectly ordinary answer and is full of them."""
    assert parse_inline_answers(["envvar.DSN=postgres://u:p@h/db?a=b"]) == {
        "envvar.DSN": "postgres://u:p@h/db?a=b"
    }


def test_a_missing_answers_file_is_a_usage_error(tmp_path: Path):
    """Carrying on without the answers would stop at the first question the file was to answer."""
    with pytest.raises(InvalidArgument):
        load_answers_file(tmp_path / "nowhere.yml")


def test_an_answers_file_must_map_keys_to_answers(tmp_path: Path):
    """A list of anything is not an answer to a named question."""
    path = tmp_path / "answers.yml"
    path.write_text("- one\n- two\n", encoding="utf-8")

    with pytest.raises(InvalidArgument):
        load_answers_file(path)


def test_an_env_file_answers_the_compose_environment_keys(tmp_path: Path):
    """A dotenv file is the natural way to hand over the values that become the compose .env."""
    path = tmp_path / ".env"
    path.write_text('DATABASE_URL="postgres://host/db"\nDEBUG=false\n', encoding="utf-8")

    assert load_env_file(path) == {
        "envvar.DATABASE_URL": "postgres://host/db",
        "envvar.DEBUG": "false",
    }


@pytest.mark.parametrize(
    "key, variable",
    [
        ("env.region", "OPSMITH_ANSWER_ENV_REGION"),
        ("envvar.DATABASE_URL", "OPSMITH_ANSWER_ENVVAR_DATABASE_URL"),
        ("env.domain.api-web", "OPSMITH_ANSWER_ENV_DOMAIN_API_WEB"),
    ],
)
def test_every_key_has_an_environment_variable_that_answers_it(key: str, variable: str):
    """
    A secret should not have to go on a command line, and the variable is how it does not. The
    rule has to be stated once, here, so a harness never has to guess it.
    """
    assert environment_variable_for(key) == variable


def test_the_sources_are_consulted_in_the_order_the_command_line_implies():
    """
    An answer typed on the command line beats a file, and any of them beats the environment,
    because that is the order of how deliberately each one was chosen for this run.
    """
    sources = AnswerSources(
        inline={"a": "from inline"},
        env_file={"a": "from env file", "b": "from env file"},
        answers_file={"a": "x", "b": "y", "c": "from answers file"},
        environ={"OPSMITH_ANSWER_A": "z", "OPSMITH_ANSWER_D": "from the environment"},
    )

    assert sources.supplied("a") == (True, "from inline", SOURCE_INLINE)
    assert sources.supplied("b") == (True, "from env file", SOURCE_ENV_FILE)
    assert sources.supplied("c") == (True, "from answers file", SOURCE_ANSWERS_FILE)
    assert sources.supplied("d") == (True, "from the environment", SOURCE_ENVIRONMENT)
    assert sources.supplied("e") == (False, None, "")
