"""Tests for declared questions and what can be worked out from them without asking.

Everything here is about the machinery rather than about any particular question, so the trees
are made up on the spot. What uses it for real is ``test_env_plan.py``.
"""

from typing import List, Optional

import pytest
import yaml

from opsmith.core.errors import OpsmithError
from opsmith.core.interaction import Choice
from opsmith.core.questions import (
    Question,
    Resolution,
    Variant,
    ask_all,
    evaluate,
    fill,
    skeleton,
)
from opsmith.tests.conftest import FakeInteraction

REGIONS = [
    Choice(label="US East (us-east-1)", value="us-east-1"),
    Choice(label="EU West (eu-west-1)", value="eu-west-1"),
]


def region_choices(resolution: Resolution) -> List[Choice]:
    """
    :param resolution: What is known, unused here.
    :return: Two fixed regions.
    """
    return REGIONS


def zones_of(resolution: Resolution) -> Optional[List[Choice]]:
    """
    :param resolution: What is known; the zones belong to the chosen region.
    :return: One choice per zone, or None when there is no account to ask with.
    """
    if resolution.account is None:
        return None
    region = resolution.get("env.region")
    return [Choice(label=f"{region}-a", value=f"{region}-a", recommended=True)]


def variables(resolution: Resolution) -> List[Variant]:
    """
    :param resolution: What is known, unused here.
    :return: The three shapes a variable comes in: plain with a default, secret without one, and
        secret with one - which is the case that tells a skeleton apart from a leak.
    """
    return [
        Variant(token="API_URL", message="Enter value for API_URL", default="http://localhost"),
        Variant(token="DB_PASSWORD", message="Enter value for DB_PASSWORD", secret=True),
        Variant(
            token="SECRET_KEY",
            message="Enter value for SECRET_KEY",
            default="not-for-a-file",
            secret=True,
        ),
    ]


# --- filling a repeated question's key --------------------------------------------------------


@pytest.mark.parametrize(
    "shape, token, expected",
    [
        ("env.domain.<slug>", "api", "env.domain.api"),
        ("envvar.<KEY>", "DATABASE_URL", "envvar.DATABASE_URL"),
        ("build_env.<slug>.<KEY>", "web.VITE_API", "build_env.web.VITE_API"),
        ("env.region", "ignored", "env.region"),
    ],
)
def test_a_repeated_key_is_completed_by_its_variant(shape, token, expected):
    """
    A key's placeholders are its tail and a variant names itself with one token, so a key with
    two placeholders is completed by one token rather than by repeating it.
    """
    assert fill(shape, token) == expected


# --- asking -----------------------------------------------------------------------------------


def test_asking_a_tree_feeds_each_answer_to_the_next_question():
    """
    The whole reason a tree is walked rather than asked all at once: the zone list belongs to the
    region that was just chosen, so the answer has to be visible to the next loader.
    """
    interact = FakeInteraction({"env.region": "eu-west-1"})
    tree = [
        Question("env.region", "Select a region", primitive="select", choices=region_choices),
        Question(
            "env.zone",
            "Select a zone",
            primitive="select",
            depends_on=("env.region",),
            choices=zones_of,
        ),
    ]

    answers = ask_all(interact, tree, Resolution(account=object()))

    assert answers == {"env.region": "eu-west-1", "env.zone": "eu-west-1-a"}


def test_a_repeated_question_is_asked_once_per_variant():
    """One declaration becomes one question per variable, each under its own key."""
    interact = FakeInteraction({"envvar.DB_PASSWORD": "hunter2"})

    answers = ask_all(
        interact, [Question("envvar.<KEY>", "Enter a value", for_each=variables)], Resolution()
    )

    assert answers == {
        "envvar.API_URL": "http://localhost",
        "envvar.DB_PASSWORD": "hunter2",
        "envvar.SECRET_KEY": "not-for-a-file",
    }
    assert [entry["secret"] for entry in interact.asked] == [False, True, True]


def test_a_question_that_does_not_apply_is_never_asked():
    """A branch pruned by `when` costs nothing: it is not asked and it is not reported."""
    interact = FakeInteraction()
    tree = [
        Question("env.project_id", "Enter a project", when=lambda _: False),
        Question("env.region", "Select a region", primitive="select", choices=region_choices),
    ]

    ask_all(interact, tree, Resolution())

    assert [entry["key"] for entry in interact.asked] == ["env.region"]


def test_a_select_with_no_options_is_reported_as_a_bug_not_a_missing_answer():
    """
    A select whose loader cannot list anything during a real run is the declaring plugin's fault.
    Reporting it as a missing answer would tell a driver to supply one of no options.
    """
    interact = FakeInteraction()
    tree = [Question("env.zone", "Select a zone", primitive="select", choices=zones_of)]

    with pytest.raises(OpsmithError) as raised:
        ask_all(interact, tree, Resolution())

    assert raised.value.details["key"] == "env.zone"


# --- evaluating -------------------------------------------------------------------------------


def test_evaluation_separates_what_is_known_from_what_is_needed():
    """An answer already given is reported as known; everything else is reported as needed."""
    tree = [
        Question("env.region", "Select a region", primitive="select", choices=region_choices),
        Question("env.name", "Enter a name"),
    ]

    evaluation = evaluate(tree, Resolution(answers={"env.region": "us-east-1"}))

    assert evaluation.known == ["env.region"]
    assert [question.key for question in evaluation.needed] == ["env.name"]


def test_an_unanswered_dependency_blocks_its_branch_rather_than_guessing_at_it():
    """
    A question whose options and wording turn on an answer that is missing is not reported at
    all: its dependency is reported as the thing to answer first, which is the round boundary.
    """
    tree = [
        Question("env.region", "Select a region", primitive="select", choices=region_choices),
        Question(
            "env.zone",
            "Select a zone",
            primitive="select",
            depends_on=("env.region",),
            choices=zones_of,
        ),
    ]

    evaluation = evaluate(tree, Resolution())

    assert [question.key for question in evaluation.needed] == ["env.region"]
    assert evaluation.blocked_on == ["env.region"]
    assert evaluation.complete is False


def test_a_question_reports_the_options_its_loader_could_list():
    """The options are reported in the form an answer names them by, not as labels."""
    tree = [Question("env.region", "Select a region", primitive="select", choices=region_choices)]

    evaluation = evaluate(tree, Resolution())

    choices = evaluation.needed[0].choices
    assert choices is not None
    assert [option.value for option in choices] == [
        "us-east-1",
        "eu-west-1",
    ]
    assert evaluation.needed[0].env_var == "OPSMITH_ANSWER_ENV_REGION"


def test_a_loader_that_cannot_list_here_leaves_the_question_unlisted():
    """
    Options that need an account are reported as not known rather than as absent, and the
    evaluation says why it is partial instead of pretending to be whole.
    """
    tree = [
        Question("env.zone", "Select a zone", primitive="select", choices=zones_of, asked_by="GCP")
    ]

    evaluation = evaluate(tree, Resolution(), tolerant=True)

    assert evaluation.needed[0].choices is None
    assert evaluation.complete is False
    assert any("env.zone" in note for note in evaluation.notes)


def test_a_failing_loader_is_a_note_when_tolerated_and_an_error_when_not():
    """
    Listing regions needs credentials a machine planning a deployment may not have. A plan
    tolerates that and says so; a run does not, because it is about to need the answer.
    """

    def refuses(resolution: Resolution) -> List[Choice]:
        """:raises RuntimeError: Always, standing in for an SDK that will not answer."""
        raise RuntimeError("no credentials")

    tree = [Question("env.region", "Select a region", primitive="select", choices=refuses)]

    tolerated = evaluate(tree, Resolution(), tolerant=True)
    assert tolerated.needed[0].choices is None
    assert "no credentials" in tolerated.notes[0]

    with pytest.raises(RuntimeError):
        evaluate(tree, Resolution())


def test_whether_a_question_is_required_follows_the_rule_the_run_applies():
    """
    Required is not declared anywhere. It is whether this run would be stopped by the question,
    which is exactly the rule a headless run applies: a default answers it only under
    --accept-defaults.
    """
    tree = [Question("envvar.<KEY>", "Enter a value", for_each=variables)]

    stopping = evaluate(tree, Resolution())
    defaulting = evaluate(tree, Resolution(), accept_defaults=True)

    assert [question.required for question in stopping.needed] == [True, True, True]
    # Only the ones with a default stop being a stop. Secrecy is about where an answer may be
    # written down, not about whether the question has one.
    assert [question.required for question in defaulting.needed] == [False, True, False]


def test_a_destructive_key_is_required_even_under_accept_defaults():
    """
    The one exception to the rule above, and the reason it is worth testing: no blanket option
    ever approves something irreversible.
    """
    tree = [Question("delete.confirm", "Type DELETE", primitive="confirm", default=True)]

    evaluation = evaluate(tree, Resolution(), accept_defaults=True)

    assert evaluation.needed[0].required is True


def test_an_answer_the_run_was_told_up_front_counts_as_known():
    """
    A plan must not report a question the run it is planning would never stop at, so it consults
    the same sources the run will - handed in as a lookup.
    """
    supplied = {"env.region": "us-east-1"}
    resolution = Resolution(
        lookup=lambda key: (key in supplied, supplied.get(key)),
    )
    tree = [
        Question("env.region", "Select a region", primitive="select", choices=region_choices),
        Question("env.name", "Enter a name"),
    ]

    evaluation = evaluate(tree, resolution)

    assert evaluation.known == ["env.region"]
    assert [question.key for question in evaluation.needed] == ["env.name"]


# --- the skeleton -----------------------------------------------------------------------------


def test_the_skeleton_fills_in_defaults_and_leaves_the_rest_commented_out():
    """
    A key present with an empty value is an answer of the empty string as far as every source is
    concerned, so anything still to be answered is written commented out. A secret is too, even
    though it has a default, because this file is written in the clear.
    """
    evaluation = evaluate(
        [Question("envvar.<KEY>", "Enter a value", for_each=variables)], Resolution()
    )

    body = skeleton(evaluation.needed)
    loaded = yaml.safe_load(body) or {}

    assert loaded == {"envvar.API_URL": "http://localhost"}
    assert "# envvar.DB_PASSWORD:" in body
    assert "# envvar.SECRET_KEY:" in body
    assert "not-for-a-file" not in body


def test_the_skeleton_names_the_options_a_question_offers():
    """A person editing the file should not have to run the plan again to see the choices."""
    evaluation = evaluate(
        [Question("env.region", "Select a region", primitive="select", choices=region_choices)],
        Resolution(),
    )

    body = skeleton(evaluation.needed)

    assert "# Select a region" in body
    assert "us-east-1, eu-west-1" in body
    assert yaml.safe_load(body) is None
