"""Questions declared as data, and what can be worked out from them before anybody is asked.

Everything Opsmith asks already has a stable key, so a flag, a file or an agent can answer it.
What a key alone cannot do is tell you the question is coming. Until this module existed a
question existed only as a call site, and the only way to discover one was to run far enough to
hit it - which for ``env create`` means discovering the fourth question after three cloud
resources have already been made.

A :class:`Question` is that call site written down: the key, how it is asked, where its options
come from and what has to be answered before it can be enumerated at all. Two things are done
with a list of them. :func:`ask_all` walks it through ``ctx.interact`` and is what a real run
uses; :func:`evaluate` walks the same list without asking anything, and is what ``opsmith env
plan`` reports.

Declaring is optional, deliberately. A provider or a strategy that declares nothing still works
through the exit-3 loop - it asks inline, wherever it needs an answer, exactly as before - and
loses nothing but its coverage in ``env plan``. That is what keeps the plugin contract small
enough to be worth implementing.
"""

import re
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Sequence,
    Tuple,
)

import yaml
from pydantic import BaseModel, Field

from opsmith.core.answers import DESTRUCTIVE_KEYS, environment_variable_for
from opsmith.core.errors import OpsmithError
from opsmith.core.events import EventSink
from opsmith.core.interaction import Choice, Interaction, choice_token

if TYPE_CHECKING:
    from opsmith.types import DeploymentConfig

#: Matches a repeated question's placeholder and everything after it: the ``<slug>`` of
#: ``env.domain.<slug>``, and the whole ``<slug>.<KEY>`` of ``build_env.<slug>.<KEY>``. The
#: placeholders are always the tail of a key, and a variant names itself with one token, so the
#: run of them is replaced as a unit. The spelling is the one the interaction key table uses.
PLACEHOLDER = re.compile(r"<[^>]+>.*$")

#: The primitives a question can be asked with. ``edit`` and ``wait_for`` are deliberately absent:
#: neither has an answer a driver can supply ahead of time, so neither belongs in a plan.
Primitive = Literal["ask", "select", "confirm"]


@dataclass(frozen=True)
class Resolution:
    """What a list of questions is evaluated against.

    It carries what is already known and the two things a loader may read to work out more: the
    repository's configuration, and the cloud account when one has been detected. ``account`` is
    None while planning on a machine with no credentials, which is the case a loader has to
    answer for by returning None rather than by failing.
    """

    #: What is answered so far, by key. Mutable, and owned by whoever is walking the list: an
    #: answer recorded here is visible to the loaders of every question after it.
    answers: Dict[str, Any] = field(default_factory=dict)

    #: Where a loader reports the wait on a slow read.
    events: Optional[EventSink] = None

    #: What the repository deploys, for the questions that are one per service.
    deployment_config: Optional["DeploymentConfig"] = None

    #: What detecting the cloud account found, when it has been detected. Typed loosely because
    #: each provider has its own, and this module knows about none of them.
    account: Optional[Any] = None

    #: Where an answer this walk has not produced itself comes from: the flags, files and store a
    #: run was given. ``env plan`` needs it, because the questions it must not report are the
    #: ones the run it is planning would never be stopped by. Returns whether there is an answer
    #: and what it is.
    lookup: Optional[Callable[[str], Tuple[bool, Any]]] = None

    def knows(self, key: str) -> bool:
        """
        :param key: The interaction key.
        :return: Whether this key already has an answer.
        """
        if key in self.answers:
            return True

        if self.lookup is None:
            return False

        found, value = self.lookup(key)
        if found:
            self.answers[key] = value
        return found

    def get(self, key: str) -> Any:
        """
        :param key: The interaction key.
        :return: The answer, or None when there is none.
        """
        self.knows(key)
        return self.answers.get(key)

    def record(self, key: str, value: Any):
        """
        Remembers an answer, so the loaders of later questions can read it.

        :param key: The interaction key.
        :param value: What was answered.
        """
        self.answers[key] = value


#: Builds the options of one question. It returns None when it cannot enumerate them here - no
#: credentials, or an answer it depends on missing - which is reported as a question whose
#: options are not yet known, rather than as a failure.
ChoiceLoader = Callable[[Resolution], Optional[List[Choice]]]

#: Decides whether a question applies at all, such as the GCP-only ones.
Predicate = Callable[[Resolution], bool]

#: Checks a typed answer, returning the problem with it or None. The same shape
#: :meth:`Interaction.ask` takes.
Validator = Callable[[str], Optional[str]]


@dataclass(frozen=True)
class Variant:
    """One instance of a question that is asked once per something.

    A repeated question declares the shape - ``envvar.<KEY>`` - and an expansion yields one of
    these per instance. Message, default and secrecy live here rather than on the question,
    because they are what differs between one environment variable and the next.
    """

    #: What replaces the placeholder in the key.
    token: str

    #: The question as this instance words it.
    message: str

    #: What this instance falls back to, when it has something to fall back to.
    default: Optional[str] = None

    #: Whether this instance's answer must not be echoed, stored in the clear or logged.
    secret: bool = False


#: Expands a repeated question into its instances. Returns an empty list when there are none.
Expansion = Callable[[Resolution], List[Variant]]


@dataclass(frozen=True)
class Question:
    """One question, written down rather than only called.

    The fields mirror the arguments of the interaction primitives, plus the three that make a
    list of these a tree: ``when`` prunes a branch, ``depends_on`` says what has to be answered
    before this one can even be enumerated, and ``for_each`` turns one declaration into one
    question per service or per variable.
    """

    #: The stable interaction key, or its shape when the question repeats: ``env.domain.<slug>``.
    key: str

    #: The question, in plain text. A repeated question's instances word it for themselves.
    message: str

    #: Which primitive asks it.
    primitive: Primitive = "ask"

    #: Who asks it - ``env create``, ``AWS``, ``Monolithic`` - so a plan can say where a question
    #: comes from. Plain text, because a third party's name is its own.
    asked_by: str = ""

    #: What it falls back to. A repeated question's default is on the variant instead.
    default: Optional[Any] = None

    #: Whether the answer must not be echoed, stored in the clear or logged.
    secret: bool = False

    #: Where the options come from, for a ``select``.
    choices: Optional[ChoiceLoader] = None

    #: Whether the question applies at all.
    when: Optional[Predicate] = None

    #: The keys that must be answered before this question can be enumerated. A plan that lacks
    #: one of them reports this question as blocked rather than guessing at it.
    depends_on: Tuple[str, ...] = ()

    #: What turns this declaration into one question per instance.
    for_each: Optional[Expansion] = None

    #: Checks a typed answer, for an ``ask``.
    validate: Optional[Validator] = None

    @property
    def repeated(self) -> bool:
        """
        :return: Whether this declaration stands for one question per instance.
        """
        return self.for_each is not None


@dataclass(frozen=True)
class Instance:
    """One concrete question: a declaration with its placeholder filled in."""

    key: str
    message: str
    default: Optional[Any]
    secret: bool
    question: Question


class ChoiceOption(BaseModel):
    """One option of a question, in the form an answer can name it by."""

    label: str = Field(..., description="What a person would see for this option.")
    value: str = Field(..., description="What to supply to choose it.")


class PlannedQuestion(BaseModel):
    """One answer a run is going to need, described for somebody who has not run it yet.

    It carries what the exit-3 stop carries, so a driver reads the same fields whether it
    discovered the question by planning or by colliding with it.
    """

    key: str = Field(..., description="The stable key this answer is addressed by.")
    message: str = Field(..., description="The question, in plain text.")
    primitive: str = Field(..., description="Which primitive asks it: ask, select or confirm.")
    asked_by: str = Field(
        "", description="What asks it: the command, the provider or the strategy."
    )
    required: bool = Field(
        ...,
        description=(
            "Whether the run will stop here. False when the question has a default that this"
            " run's options would take."
        ),
    )
    secret: bool = Field(
        False, description="Whether the answer must not be put on a command line or in a file."
    )
    default: Optional[str] = Field(None, description="What it falls back to, when it has one.")
    choices: Optional[List[ChoiceOption]] = Field(
        None,
        description=(
            "The options to choose between. Null means they are not known here - a listing that"
            " needs credentials, or an earlier answer - not that there are none."
        ),
    )
    env_var: str = Field(..., description="The environment variable that answers this key.")


@dataclass(frozen=True)
class Evaluation:
    """What walking a list of questions without asking them worked out."""

    #: The questions this run will stop for, or would stop for without its defaults.
    needed: List[PlannedQuestion] = field(default_factory=list)

    #: The keys that already have an answer, by name only. Values are never collected: some of
    #: them are secrets, and a plan is printed.
    known: List[str] = field(default_factory=list)

    #: The keys that have to be answered before the rest of the list can be enumerated.
    blocked_on: List[str] = field(default_factory=list)

    #: Why something could not be worked out, in plain text. A region listing that needed
    #: credentials the machine does not have leaves a note here and an unlisted question above.
    notes: List[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """
        :return: Whether everything this list declares could be enumerated.
        """
        return not self.blocked_on and not self.notes


def fill(key: str, token: str) -> str:
    """
    Replaces the placeholders of a repeated question's key with what this instance is about.

    :param key: The key's shape, such as ``env.domain.<slug>`` or ``build_env.<slug>.<KEY>``.
    :param token: What completes it: a service slug, or ``<slug>.<KEY>`` joined as the key
        spells it.
    :return: The concrete key.
    """
    # A lambda rather than a plain string, because a token is user data and re.sub reads
    # backslashes in a replacement string as group references.
    return PLACEHOLDER.sub(lambda _: token, key)


def instances_of(question: Question, resolution: Resolution) -> List[Instance]:
    """
    Turns one declaration into the concrete questions it stands for.

    :param question: The declaration.
    :param resolution: What is known, for an expansion that reads the configuration.
    :return: One instance for an ordinary question, one per variant for a repeated one.
    """
    if question.for_each is None:
        return [
            Instance(
                key=question.key,
                message=question.message,
                default=question.default,
                secret=question.secret,
                question=question,
            )
        ]

    return [
        Instance(
            key=fill(question.key, variant.token),
            message=variant.message,
            default=variant.default,
            secret=variant.secret or question.secret,
            question=question,
        )
        for variant in question.for_each(resolution)
    ]


def applies(question: Question, resolution: Resolution) -> bool:
    """
    :param question: The declaration.
    :param resolution: What is known.
    :return: Whether this question is asked at all, given what has been answered so far.
    """
    return question.when is None or question.when(resolution)


def unmet_dependencies(question: Question, resolution: Resolution) -> List[str]:
    """
    :param question: The declaration.
    :param resolution: What is known.
    :return: The keys this question needs answered before it can be enumerated, that are not.
    """
    return [key for key in question.depends_on if not resolution.knows(key)]


def ask_all(
    interact: Interaction, questions: Sequence[Question], resolution: Resolution
) -> Dict[str, Any]:
    """
    Asks every question a list declares, in the order it declares them.

    Each answer is recorded into the resolution before the next question is reached, which is
    what lets one question's options depend on another's answer - GCP lists the regions of the
    project that was just named. Nothing here decides where an answer comes from: a headless run
    resolves it from what it was told and stops when it cannot, exactly as it does for a question
    asked inline.

    :param interact: How this run reaches a person, or stands in for one.
    :param questions: The declarations to walk.
    :param resolution: What is known, and where each answer is recorded.
    :return: The answers, by concrete key.
    :raises OpsmithError: A select declared no way to build its options, which is a bug in
        whatever declared it rather than anything a person can answer.
    """
    answered: Dict[str, Any] = {}

    for question in questions:
        if not applies(question, resolution):
            continue

        for instance in instances_of(question, resolution):
            value = _ask_one(interact, instance, resolution)
            resolution.record(instance.key, value)
            answered[instance.key] = value

    return answered


def _ask_one(interact: Interaction, instance: Instance, resolution: Resolution) -> Any:
    """
    Asks one concrete question through the primitive it declared.

    :param interact: How this run reaches a person.
    :param instance: The question, with its placeholder filled in.
    :param resolution: What is known, for the loader that builds the options.
    :return: The answer.
    :raises OpsmithError: A select whose loader could not build its options during a real run.
    """
    question = instance.question

    if question.primitive == "select":
        choices = question.choices(resolution) if question.choices is not None else None
        if not choices:
            raise OpsmithError(
                f"There are no options to answer '{instance.key}' with.",
                hint=(
                    "This is a bug in the provider or strategy that declared the question: a"
                    " select must be able to list what it is choosing between."
                ),
                details={"key": instance.key},
            )
        return interact.select(instance.key, instance.message, choices, default=instance.default)

    if question.primitive == "confirm":
        return interact.confirm(instance.key, instance.message, default=bool(instance.default))

    return interact.ask(
        instance.key,
        instance.message,
        default=instance.default,
        secret=instance.secret,
        validate=question.validate,
    )


def evaluate(
    questions: Sequence[Question],
    resolution: Resolution,
    *,
    accept_defaults: bool = False,
    tolerant: bool = False,
) -> Evaluation:
    """
    Works out what a list of questions will ask, without asking any of it.

    A question whose dependencies are unanswered is not enumerated at all: its options, its
    wording and even whether it applies may all turn on the answer that is missing, so reporting
    it would mean inventing it. Its key is reported as something to answer first instead, which
    is the round boundary - answer it, run the plan again, and the branch it opens is enumerated.

    :param questions: The declarations to walk.
    :param resolution: What is known, and what a loader may read.
    :param accept_defaults: Whether this run would take a question's own default, which is what
        decides whether a question with one is going to stop it.
    :param tolerant: Whether a loader that fails should leave the question unlisted instead of
        stopping the walk. ``opsmith env plan`` sets it, because listing regions needs
        credentials a machine planning a deployment may not have yet, and a plan that could not
        list them is still worth having.
    :return: What is needed, what is known, what is blocking the rest, and what went unlisted.
    """
    evaluation = Evaluation()

    for question in questions:
        unmet = unmet_dependencies(question, resolution)
        if unmet:
            for key in unmet:
                if key not in evaluation.blocked_on:
                    evaluation.blocked_on.append(key)
            continue

        if not applies(question, resolution):
            continue

        choices = _planned_choices(question, resolution, evaluation, tolerant=tolerant)
        for instance in instances_of(question, resolution):
            if resolution.knows(instance.key):
                evaluation.known.append(instance.key)
                continue
            evaluation.needed.append(_planned(instance, choices, accept_defaults=accept_defaults))

    return evaluation


def _planned_choices(
    question: Question,
    resolution: Resolution,
    evaluation: Evaluation,
    *,
    tolerant: bool,
) -> Optional[List[ChoiceOption]]:
    """
    Builds the options to report for a question, where they can be worked out here.

    :param question: The declaration.
    :param resolution: What is known, and what a loader may read.
    :param evaluation: Where a note goes when the options could not be listed.
    :param tolerant: Whether a failing loader is a note rather than the end of the walk.
    :return: The options, or None when they could not be listed from here.
    :raises Exception: Whatever the loader raised, when not tolerating failures.
    """
    if question.choices is None:
        return None

    if not tolerant:
        loaded = question.choices(resolution)
        return None if loaded is None else _as_options(loaded)

    # The one place a bare except is right: every loader is somebody else's code reaching a
    # cloud, and a plan is worth printing without the region list in it.
    try:
        loaded = question.choices(resolution)
    except Exception as failure:  # noqa: BLE001 - deliberate, see above
        evaluation.notes.append(f"Could not list the options for '{question.key}': {failure}")
        return None

    if loaded is None:
        evaluation.notes.append(
            f"The options for '{question.key}' are only known once the run has an account."
        )
    return None if loaded is None else _as_options(loaded)


def _as_options(choices: List[Choice]) -> List[ChoiceOption]:
    """
    :param choices: What a loader returned.
    :return: The same options, each named the way an answer can name it.
    """
    return [ChoiceOption(label=choice.label, value=str(choice_token(choice))) for choice in choices]


def _planned(
    instance: Instance,
    choices: Optional[List[ChoiceOption]],
    *,
    accept_defaults: bool,
) -> PlannedQuestion:
    """
    Describes one concrete question for somebody who has not run the command yet.

    Whether it is required is not declared anywhere: it is the rule
    :class:`~opsmith.core.interaction.HeadlessInteraction` already applies, which is that a
    default answers a question only under ``--accept-defaults``, and never for a key that gates
    something irreversible.

    :param instance: The question, with its placeholder filled in.
    :param choices: The options, when they could be worked out.
    :param accept_defaults: Whether this run would take the question's own default.
    :return: The description.
    """
    takes_default = (
        accept_defaults and instance.default is not None and instance.key not in DESTRUCTIVE_KEYS
    )

    return PlannedQuestion(
        key=instance.key,
        message=instance.message,
        primitive=instance.question.primitive,
        asked_by=instance.question.asked_by,
        required=not takes_default,
        secret=instance.secret,
        default=None if instance.default is None else str(instance.default),
        choices=choices,
        env_var=environment_variable_for(instance.key),
    )


def skeleton(needed: Sequence[PlannedQuestion]) -> str:
    """
    Writes the answers file a driver edits and hands back through ``--answers``.

    A question that has a default is written with it, so the file is usable as it stands. One
    that has to be answered is written commented out, under the question it answers: a key
    present with an empty value is an answer of the empty string as far as every source is
    concerned, and would satisfy the question it was meant to leave open. A secret is always
    commented out, because this file is written in the clear.

    :param needed: The questions to write, from an evaluation.
    :return: The file's contents.
    """
    lines = [
        "# Answers for opsmith, written by 'opsmith env plan --write-answers'.",
        "# Fill in each commented key and pass the file back with --answers.",
        "# A secret is left commented out on purpose: put those in a --env-file instead.",
        "",
    ]

    for question in needed:
        lines.append(f"# {question.message}")
        if question.choices:
            offered = ", ".join(option.value for option in question.choices)
            lines.append(f"# one of: {offered}")

        if question.default is None or question.secret:
            lines.append(f"# {question.key}:")
        else:
            # Dumped rather than formatted, so a default holding a colon, a hash or a leading
            # zero comes back out of the file as the same string it went in as.
            lines.append(
                yaml.safe_dump(
                    {question.key: question.default}, default_flow_style=False, sort_keys=False
                ).strip()
            )
        lines.append("")

    return "\n".join(lines)
