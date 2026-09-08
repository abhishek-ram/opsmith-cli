"""Where provisioners come from.

Strategies used to build a ``TerraformProvisioner`` or an ``AnsibleProvisioner`` wherever they
needed one, which meant every one of them reached the filesystem and shelled out to a real tool.
They ask the factory instead, so a test can hand a strategy fakes and assert on the variables it
would have applied.
"""

from pathlib import Path
from typing import Optional

from opsmith.core.events import STEP_PROVISION, EventSink
from opsmith.infra_provisioners.ansible_provisioner import AnsibleProvisioner
from opsmith.infra_provisioners.terraform_provisioner import TerraformProvisioner

#: The templates shipped with the package. The factory holds it so a provisioner does not have to
#: work out where it lives, and so a test can point the whole run at a different tree.
PACKAGE_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


class ProvisionerFactory:
    """Builds provisioners wired to this run's event sink and template tree."""

    def __init__(self, events: EventSink, templates_dir: Optional[Path] = None):
        """
        :param events: The sink every provisioner this factory builds reports to.
        :param templates_dir: Root of the template tree, defaulting to the packaged templates.
        """
        self.events = events
        self.templates_dir = templates_dir if templates_dir is not None else PACKAGE_TEMPLATES_DIR

    def terraform(self, working_dir: Path, step: str = STEP_PROVISION) -> TerraformProvisioner:
        """
        Builds a terraform provisioner for a working directory.

        :param working_dir: Directory the configuration lives and runs in. Created if missing.
        :param step: The step name the provisioner's events carry.
        :return: A ready provisioner.
        """
        return TerraformProvisioner(
            working_dir=working_dir,
            events=self.events,
            step=step,
            templates_dir=self.templates_dir,
        )

    def ansible(self, working_dir: Path, step: str = STEP_PROVISION) -> AnsibleProvisioner:
        """
        Builds an ansible provisioner for a working directory.

        :param working_dir: Directory the playbook lives and runs in. Created if missing.
        :param step: The step name the provisioner's events carry.
        :return: A ready provisioner.
        """
        return AnsibleProvisioner(
            working_dir=working_dir,
            events=self.events,
            step=step,
            templates_dir=self.templates_dir,
        )
