"""Where an answer comes from, and where it goes once it is given.

A question is asked once. After that the answer belongs to the environment, not to the run that
happened to ask, which is what lets a headless run stop for one missing answer and pick up exactly
where it left off. This module holds both halves of that: the sources an answer may arrive from,
and the store it is written to the moment it is given.

The store has two halves behind one interface. Ordinary answers go to ``answers.yml``, a flat
mapping of key to value so the file the store writes is the same shape the ``--answers`` flag
reads. Secrets go to a private cache beside it, and - for the monolithic strategy - are superseded
by the ``.env`` on the virtual machine as soon as there is one, because that file is the real
secret store and this part adopts it rather than building a second.

None of it lives in the repository. What a repository holds is what somebody wrote, plus the state
of what is deployed; what is remembered here was neither written nor deployed. Keeping it out is
also the only way to promise the secret half is never committed, since an ignore rule does not
survive ``git add -f``, an archive of the directory, or a build context that never read it.
"""

import os
import re
import stat
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

import yaml
from dotenv import dotenv_values

from opsmith.core.errors import InvalidArgument
from opsmith.core.events import STEP_INTERACT, EventSink, resolve_sink

#: The word the deletion gate asks to be typed. Declared here so the command that asks and the
#: headless resolver that stands in for the typing cannot disagree about it.
DELETE_CONFIRMATION = "DELETE"

#: What ``--yes`` answers, per key that gates something destructive. It is a mapping rather than a
#: set because not every gate is a yes or no question: the deletion gate is an ``ask`` that wants a
#: word typed, and a terminal keeps that friction.
DESTRUCTIVE_ANSWERS: Dict[str, Any] = {
    "delete.confirm": DELETE_CONFIRMATION,
    "update.confirm_infra_changes": True,
}

#: Keys whose answers must never be persisted. A menu choice describes one invocation, not the
#: environment; a destructive confirmation persisted once would silently destroy on every run
#: after it.
TRANSIENT_KEYS: Set[str] = {
    "setup.action",
    "env.action",
    "run.service",
    "run.command",
} | set(DESTRUCTIVE_ANSWERS)

#: Prefix of the environment variable that answers a key.
ENVIRONMENT_PREFIX = "OPSMITH_ANSWER_"

#: Keys under this prefix are the compose ``.env`` body: the ones ``--env-file`` answers, and the
#: ones the virtual machine becomes the source of truth for.
ENV_VAR_PREFIX = "envvar."

#: The file name of the answer store, and of the secret cache beside it. Both live under the
#: project's state directory, which :func:`opsmith.utils.project_state_dir` resolves.
ANSWERS_FILE = "answers.yml"
SECRETS_FILE = "secrets.yml"

# Labels naming where an answer came from, for the error that reports one Opsmith cannot use.
SOURCE_INLINE = "--answer"
SOURCE_ENV_FILE = "--env-file"
SOURCE_ANSWERS_FILE = "--answers"
SOURCE_ENVIRONMENT = "environment"
SOURCE_STORE = "the answer store"
SOURCE_DEFAULT = "--accept-defaults"
SOURCE_YES = "--yes"


def environment_variable_for(key: str) -> str:
    """
    Returns the environment variable that answers an interaction key.

    :param key: The interaction key, such as ``env.region``.
    :return: The variable name, such as ``OPSMITH_ANSWER_ENV_REGION``.
    """
    return ENVIRONMENT_PREFIX + re.sub(r"[.\-]", "_", key).upper()


def parse_inline_answers(pairs: List[str]) -> Dict[str, str]:
    """
    Reads the repeatable ``--answer key=value`` option.

    :param pairs: The raw strings the option collected.
    :return: The answers, by key.
    :raises InvalidArgument: One of them is not a ``key=value`` pair.
    """
    answers: Dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key.strip():
            raise InvalidArgument(
                f"--answer must be given as key=value, not '{pair}'.",
                hint="For example: --answer env.region=us-east-1",
                details={"answer": pair},
            )
        answers[key.strip()] = value
    return answers


def load_answers_file(path: Path) -> Dict[str, Any]:
    """
    Reads a YAML file mapping interaction keys to answers.

    :param path: The file named by ``--answers``.
    :return: The answers, by key.
    :raises InvalidArgument: The file is missing, unreadable, or not a mapping.
    """
    if not path.exists():
        raise InvalidArgument(
            f"Answers file '{path}' does not exist.",
            hint="Point --answers at a YAML file mapping each prompt key to its answer.",
            details={"path": str(path)},
        )

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as err:
        raise InvalidArgument(
            f"Answers file '{path}' is not valid YAML.",
            hint="Fix the file, or remove --answers.",
            details={"path": str(path), "problem": str(err)},
        ) from err

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise InvalidArgument(
            f"Answers file '{path}' must hold a mapping of prompt key to answer.",
            hint="For example:\n  env.region: us-east-1",
            details={"path": str(path)},
        )
    return {str(key): value for key, value in loaded.items()}


def load_env_file(path: Path) -> Dict[str, str]:
    """
    Reads a dotenv file into the ``envvar.<KEY>`` answers it supplies.

    :param path: The file named by ``--env-file``.
    :return: The answers, by interaction key.
    :raises InvalidArgument: The file is missing.
    """
    if not path.exists():
        raise InvalidArgument(
            f"Env file '{path}' does not exist.",
            hint="Point --env-file at a dotenv file, or remove the option.",
            details={"path": str(path)},
        )

    values = dotenv_values(str(path))
    return {f"{ENV_VAR_PREFIX}{key}": value for key, value in values.items() if value is not None}


@dataclass(frozen=True)
class AnswerSources:
    """Everything a run was told up front, in the order it is consulted.

    The persisted store and the question's own default are not here: they need the store and the
    question, and only the interaction has both. This is the part that is pure data.
    """

    inline: Dict[str, str] = field(default_factory=dict)
    env_file: Dict[str, str] = field(default_factory=dict)
    answers_file: Dict[str, Any] = field(default_factory=dict)
    environ: Mapping[str, str] = field(default_factory=dict)
    accept_defaults: bool = False
    assume_yes: bool = False

    @classmethod
    def load(
        cls,
        *,
        inline: Optional[List[str]] = None,
        answers_path: Optional[Path] = None,
        env_path: Optional[Path] = None,
        accept_defaults: bool = False,
        assume_yes: bool = False,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "AnswerSources":
        """
        Reads every source the command line named, failing on any that cannot be read.

        :param inline: The raw ``--answer key=value`` strings.
        :param answers_path: The file named by ``--answers``.
        :param env_path: The file named by ``--env-file``.
        :param accept_defaults: Whether a question may fall back to its own default.
        :param assume_yes: Whether destructive confirmations are accepted up front.
        :param environ: The environment to read ``OPSMITH_ANSWER_*`` from. Defaults to the process
            environment; supplied by tests that must not depend on one.
        :return: The sources, ready to be consulted.
        :raises InvalidArgument: A file is missing, or an inline answer is malformed.
        """
        return cls(
            inline=parse_inline_answers(inline or []),
            env_file=load_env_file(env_path) if env_path else {},
            answers_file=load_answers_file(answers_path) if answers_path else {},
            environ=environ if environ is not None else os.environ,
            accept_defaults=accept_defaults,
            assume_yes=assume_yes,
        )

    def supplied(self, key: str) -> Tuple[bool, Any, str]:
        """
        Finds what the run was told about one key, in precedence order.

        :param key: The interaction key being answered.
        :return: Whether an answer was found, the answer, and the source that carried it.
        """
        if key in self.inline:
            return True, self.inline[key], SOURCE_INLINE
        if key in self.env_file:
            return True, self.env_file[key], SOURCE_ENV_FILE
        if key in self.answers_file:
            return True, self.answers_file[key], SOURCE_ANSWERS_FILE

        variable = environment_variable_for(key)
        if variable in self.environ:
            return True, self.environ[variable], SOURCE_ENVIRONMENT

        return False, None, ""


class AnswerStore:
    """The answers an environment has already given, and where the next one is written.

    It is built before a run knows which environment it is about, because the question that names
    the environment is itself an answer. Until :meth:`use_environment` binds it, answers are held
    in memory and flushed on binding; a command that never names an environment - ``setup``, whose
    durable output is ``deployments.yml`` itself - never binds, and nothing it collects is written.
    """

    def __init__(self, root: Path, events: Optional[EventSink] = None):
        """
        :param root: The project's state directory, outside its repository.
        :param events: Where the store reports what it wrote. Optional, because a store is pure
            data until something records.
        """
        self.root = root
        self.events = resolve_sink(events)
        self.environment: Optional[str] = None

        # Ordinary answers, and the secrets held locally until the machine that owns them exists.
        self._plain: Dict[str, Any] = {}
        self._cache: Dict[str, Any] = {}

        # Secrets read back off the deployed machine. They win over the local cache, because the
        # machine is what the application is actually running with.
        self._remote: Dict[str, Any] = {}

    @property
    def bound(self) -> bool:
        """Whether the store knows which environment it is writing for."""
        return self.environment is not None

    @property
    def answers_path(self) -> Optional[Path]:
        """Where the ordinary answers are kept, once the store is bound."""
        return self._environment_dir / ANSWERS_FILE if self.bound else None

    @property
    def secrets_path(self) -> Optional[Path]:
        """Where the local secret cache is kept, once the store is bound."""
        return self._environment_dir / SECRETS_FILE if self.bound else None

    @property
    def _environment_dir(self) -> Path:
        """The environment's directory. Only meaningful once bound."""
        return self.root / "environments" / str(self.environment)

    def use_environment(self, name: str):
        """
        Binds the store to an environment, reading what it already holds and flushing the buffer.

        :param name: The environment the run is about.
        :raises InvalidArgument: The store is already bound to a different environment, which would
            mean one run had written answers for two.
        """
        if self.environment == name:
            return
        if self.bound:
            raise InvalidArgument(
                f"This run already answered for environment '{self.environment}'.",
                hint="Run one environment at a time.",
                details={"bound_to": self.environment, "requested": name},
            )

        self.environment = name

        # Worth saying once, because this no longer lives anywhere the user would think to look.
        if not self._environment_dir.exists():
            self.events.log(
                STEP_INTERACT,
                f"Answers for '{name}' will be remembered in {self._environment_dir}.",
                path=str(self._environment_dir),
            )

        buffered_plain, buffered_cache = self._plain, self._cache
        self._plain = _read_mapping(self.answers_path)
        self._cache = _read_mapping(self.secrets_path)
        self._plain.update(buffered_plain)
        self._cache.update(buffered_cache)

        if buffered_plain or buffered_cache:
            self.save()

    def has(self, key: str) -> bool:
        """
        :param key: The interaction key.
        :return: Whether the store holds an answer for it.
        """
        return key in self._remote or key in self._cache or key in self._plain

    def get(self, key: str) -> Optional[Any]:
        """
        Returns the answer already given for a key, if there is one.

        The deployed machine wins over the local cache, and the cache over the plain file, so a
        secret that has been read back off a running environment is the one that is used.

        :param key: The interaction key.
        :return: The answer, or None when the key has never been answered.
        """
        for layer in (self._remote, self._cache, self._plain):
            if key in layer:
                return layer[key]
        return None

    def record(self, key: str, value: Any, *, secret: bool = False):
        """
        Writes an answer through to disk, the moment it is given.

        Writing through rather than at the end of the run is the whole point: a run that stops for
        the next missing answer must not lose the ones it already has.

        :param key: The interaction key being answered.
        :param value: The answer.
        :param secret: Whether the value must not be written to the plain file.
        """
        if key in TRANSIENT_KEYS:
            return

        if secret:
            self._cache[key] = value
        else:
            self._plain[key] = value

        if self.bound:
            self.save()

    def adopt_env_file(self, content: str):
        """
        Takes the ``.env`` read back off a deployed machine as the source of truth for its keys.

        The local cache only exists to carry secrets until there is a machine holding them. Once
        there is, its ``envvar.*`` entries are redundant and are dropped - but nothing else is:
        build-time values never reach that file, and dropping them would lose them.

        :param content: The dotenv body fetched from the machine.
        """
        values = dotenv_values(stream=StringIO(content))
        self._remote = {
            f"{ENV_VAR_PREFIX}{key}": value for key, value in values.items() if value is not None
        }

        stale = [key for key in self._cache if key.startswith(ENV_VAR_PREFIX)]
        if stale:
            for key in stale:
                del self._cache[key]
            if self.bound:
                self.save()

    def save(self):
        """
        Writes both halves of the store.

        Kept as one method because it is the point a later phase uploads from, rather than because
        anything here defers a write.
        """
        if not self.bound:
            return

        _make_private(self.root)
        self._environment_dir.mkdir(parents=True, exist_ok=True)
        _write_mapping(self.answers_path, self._plain)

        if self._cache:
            _write_mapping(self.secrets_path, self._cache, private=True)


def _read_mapping(path: Optional[Path]) -> Dict[str, Any]:
    """
    Reads one of the store's YAML files.

    :param path: The file, or None when the store is not bound.
    :return: What it held, or an empty mapping when there is nothing to read.
    """
    if path is None or not path.exists():
        return {}

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return {}
    return {str(key): value for key, value in loaded.items()}


def _make_private(root: Path):
    """
    Makes sure the project's state directory is readable only by the person who owns it.

    :param root: The directory Opsmith keeps this project's state in.
    """
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, stat.S_IRWXU)


def _write_mapping(path: Path, values: Dict[str, Any], *, private: bool = False):
    """
    Writes one of the store's YAML files.

    :param path: Where to write.
    :param values: The mapping to write.
    :param private: Whether the file holds secrets, and so must be readable only by its owner.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if private and not path.exists():
        # Created before the write so the values are never briefly world readable.
        path.touch(mode=stat.S_IRUSR | stat.S_IWUSR)

    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(values, handle, default_flow_style=False, sort_keys=True)

    if private:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
