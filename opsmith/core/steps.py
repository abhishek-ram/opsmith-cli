"""A ledger for the steps that cannot simply be run again.

Every command Opsmith runs must be safe to run again, because that is what makes a headless run
resumable: it stops for one missing answer, and the next invocation repeats what it already did and
carries on. Terraform and the playbooks are idempotent, so almost nothing needs this.

What needs it is a step that is not - one that would create a second thing if it ran twice. A
strategy wraps such a step and the ledger remembers it:

    with ctx.steps.once("vm.create") as should_run:
        if should_run:
            ...

The two-part shape is not decoration. A context manager cannot skip its own body: Python runs the
block regardless, and an exception raised on the way in propagates instead of being swallowed. So
the ledger reports whether the block should run and the caller honours it.

This is a convenience for strategy authors, not part of the strategy contract. A third-party
strategy that never touches it works exactly as well, as long as its steps are idempotent.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, Optional

import yaml

from opsmith.core.errors import InvalidArgument

#: Where the ledger is kept, under the project's state directory. Deliberately not `state.yml`:
#: that file existing is what tells the rest of Opsmith an environment has been deployed, and the
#: ledger has to persist mid-run, long before that is true. It is also not in the repository -
#: which step a run happens to have reached is nobody's business but this machine's.
STEPS_FILE = "steps.yml"


class StepLedger:
    """Remembers which non-idempotent steps an environment has already completed."""

    def __init__(self, root: Path):
        """
        :param root: The project's state directory, outside its repository.
        """
        self.root = root
        self.environment: Optional[str] = None
        self._completed: Dict[str, str] = {}

    @property
    def bound(self) -> bool:
        """Whether the ledger knows which environment it is recording for."""
        return self.environment is not None

    @property
    def path(self) -> Optional[Path]:
        """Where the ledger is kept, once it is bound."""
        if not self.bound:
            return None
        return self.root / "environments" / str(self.environment) / STEPS_FILE

    def use_environment(self, name: str):
        """
        Binds the ledger to an environment and reads what it already recorded.

        :param name: The environment the run is about.
        :raises InvalidArgument: The ledger is already bound to a different environment.
        """
        if self.environment == name:
            return
        if self.bound:
            raise InvalidArgument(
                f"This run is already recording steps for environment '{self.environment}'.",
                hint="Run one environment at a time.",
                details={"bound_to": self.environment, "requested": name},
            )

        self.environment = name
        self._completed = self._read()

    def completed(self, step: str) -> bool:
        """
        :param step: The step name, such as ``vm.create``.
        :return: Whether it has already run to completion for this environment.
        """
        return step in self._completed

    @contextmanager
    def once(self, step: str) -> Iterator[bool]:
        """
        Reports whether a step still needs to run, and records it when it finishes.

        Nothing is recorded if the block raises, so a step that failed halfway runs again.

        :param step: The step name, such as ``vm.create``.
        :return: True when the caller should run the step, False when it is already done.
        """
        if self.completed(step):
            yield False
            return

        yield True

        self._completed[step] = datetime.now(timezone.utc).isoformat()
        self._write()

    def _read(self) -> Dict[str, str]:
        """
        :return: What the ledger file holds, or nothing when there is no file yet.
        """
        path = self.path
        if path is None or not path.exists():
            return {}

        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return {}
        return {str(key): str(value) for key, value in loaded.items()}

    def _write(self):
        """Writes the ledger, if there is an environment to write it for."""
        path = self.path
        if path is None:
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(self._completed, handle, default_flow_style=False, sort_keys=True)
