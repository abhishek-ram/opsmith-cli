"""Runs ansible playbooks in a working directory prepared from a template.

Extra variables reach the playbook through a file rather than the command line. They carry the
whole compose ``.env`` body, and anything on a command line is readable by every process on the
machine and is repeated in the details of a failure - which ``no_log`` on the remote task does
nothing about, because the exposure is local.
"""

import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Union

from opsmith.core.errors import AnsibleFailed
from opsmith.core.events import EventSink
from opsmith.infra_provisioners.base_provisioner import BaseInfrastructureProvisioner


class AnsibleProvisioner(BaseInfrastructureProvisioner):
    """A wrapper for running ansible-playbook commands."""

    def __init__(self, working_dir: Path, events: EventSink, step: str, templates_dir: Path):
        """
        :param working_dir: Directory the playbook lives and runs in.
        :param events: Sink the streamed output is reported to.
        :param step: The step name every event from this provisioner carries.
        :param templates_dir: Root of the template tree to copy playbooks from.
        """
        super().__init__(
            working_dir=working_dir,
            command_name="Ansible",
            executable="ansible-playbook",
            events=events,
            step=step,
            templates_dir=templates_dir,
            error_class=AnsibleFailed,
        )
        ansible_cfg_path = self.working_dir / "ansible.cfg"
        with open(ansible_cfg_path, "w", encoding="utf-8") as f:
            f.write("[defaults]\n")
            f.write("host_key_checking = False\n")
            f.write("deprecation_warnings = False\n")

    def run_playbook(
        self,
        playbook_name: str,
        extra_vars: Dict[str, Union[str, List[str]]],
        inventory: Optional[str] = None,
        user: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        Runs an ansible playbook.

        :param playbook_name: The playbook file inside the working directory.
        :param extra_vars: Variables handed to the playbook.
        :param inventory: A single host to run against, if not using ``inventory.yml``.
        :param user: The SSH user to connect as, if not the playbook's default.
        :return: The values the run handed back through ``OPSMITH_OUTPUT_`` markers.
        :raises AnsibleFailed: The playbook is missing, or the run exited non-zero.
        """
        playbook_path = self.working_dir / playbook_name
        if not playbook_path.exists():
            raise AnsibleFailed(
                f"Playbook '{playbook_name}' not found in {self.working_dir}.",
                hint="The template for this step did not provide the playbook it should have.",
                details={"playbook": str(playbook_path)},
            )

        command = ["ansible-playbook", str(playbook_path)]
        if inventory:
            # The comma is important for a single host inventory
            command.extend(["-i", f"{inventory},"])
        elif (self.working_dir / "inventory.yml").exists():
            command.extend(["-i", "inventory.yml"])

        if user:
            command.extend(["--user", user])

        with _extra_vars_file(extra_vars) as vars_path:
            if vars_path is not None:
                command.extend(["--extra-vars", f"@{vars_path}"])
            return self._run_command(command)


@contextmanager
def _extra_vars_file(extra_vars: Dict[str, Union[str, List[str]]]) -> Iterator[Optional[Path]]:
    """
    Writes the extra variables somewhere only this user can read, for the length of the run.

    The file is removed on the way out, including when the playbook fails, so nothing is left at
    rest holding an application's secrets. That does mean the command recorded in a failure cannot
    be pasted back into a shell verbatim; the working directory it names is still where to look.

    :param extra_vars: The variables the playbook needs.
    :return: The file to pass as ``--extra-vars @file``, or None when there are no variables.
    """
    if not extra_vars:
        yield None
        return

    handle, name = tempfile.mkstemp(prefix="opsmith-vars-", suffix=".json")
    path = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as vars_file:
            json.dump(extra_vars, vars_file)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        yield path
    finally:
        path.unlink(missing_ok=True)
