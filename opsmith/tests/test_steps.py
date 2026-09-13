"""The step ledger: the one thing that must not happen twice when a run happens twice.

Almost nothing needs this. Terraform and the playbooks are idempotent, so a resumed run repeating
them changes nothing. What needs it is a step that would create a second thing, and the ledger is
how a strategy says so.
"""

from pathlib import Path

import pytest
import yaml

from opsmith.core.errors import InvalidArgument
from opsmith.core.steps import STEPS_FILE, StepLedger


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """The project's state directory, which is outside the project."""
    return tmp_path / "opsmith-state"


@pytest.fixture
def ledger(state_dir: Path) -> StepLedger:
    """A ledger bound to one environment."""
    ledger = StepLedger(state_dir)
    ledger.use_environment("prod")
    return ledger


def test_a_step_runs_the_first_time(ledger: StepLedger):
    """Nothing has been recorded, so the block is the caller's to run."""
    ran = []

    with ledger.once("vm.create") as should_run:
        if should_run:
            ran.append("created")

    assert ran == ["created"]
    assert ledger.completed("vm.create")


def test_the_next_run_skips_what_the_last_one_finished(state_dir: Path):
    """
    A run stopped for a missing answer is run again from the top. Everything idempotent repeats
    harmlessly; this is how the one thing that would not says so.
    """
    first = StepLedger(state_dir)
    first.use_environment("prod")
    with first.once("vm.create") as should_run:
        assert should_run

    second = StepLedger(state_dir)
    second.use_environment("prod")

    ran = []
    with second.once("vm.create") as should_run:
        if should_run:
            ran.append("created twice")

    assert ran == []


def test_a_step_that_failed_halfway_runs_again(ledger: StepLedger):
    """Recording a step that raised would strand the environment: half made, and never retried."""
    with pytest.raises(RuntimeError):
        with ledger.once("vm.create") as should_run:
            assert should_run
            raise RuntimeError("the cloud said no")

    assert not ledger.completed("vm.create")


def test_two_steps_are_tracked_apart(ledger: StepLedger):
    """One finished step says nothing about another."""
    with ledger.once("vm.create") as should_run:
        assert should_run

    with ledger.once("registry.create") as should_run:
        assert should_run

    assert ledger.completed("vm.create")
    assert ledger.completed("registry.create")


def test_the_ledger_is_kept_outside_the_project_and_apart_from_its_state(ledger: StepLedger):
    """
    Which step a run reached is this machine's business, not the repository's. It is also
    deliberately not `state.yml`: that file existing is what tells the rest of Opsmith an
    environment has been deployed, and this has to be written long before that is true.
    """
    with ledger.once("vm.create") as should_run:
        assert should_run

    path = ledger.root / "environments" / "prod" / STEPS_FILE
    assert path.exists()
    assert "vm.create" in yaml.safe_load(path.read_text(encoding="utf-8"))
    assert not (path.parent / "state.yml").exists()


def test_nothing_is_recorded_for_a_run_that_names_no_environment(state_dir: Path):
    """An unbound ledger still runs the block; it just has nowhere to remember it."""
    ledger = StepLedger(state_dir)

    with ledger.once("vm.create") as should_run:
        assert should_run

    assert not state_dir.exists()


def test_a_run_records_steps_for_one_environment_only(ledger: StepLedger):
    """The same reason the answer store refuses: one run, one environment."""
    with pytest.raises(InvalidArgument):
        ledger.use_environment("staging")
