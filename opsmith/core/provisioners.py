"""Where provisioners come from.

Strategies used to build a ``TerraformProvisioner`` or an ``AnsibleProvisioner`` wherever they
needed one, which meant every one of them reached the filesystem and shelled out to a real tool.
They ask the factory instead, so a test can hand a strategy fakes and assert on the variables it
would have applied.
"""

from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol

from opsmith.core.events import STEP_PROVISION, EventSink
from opsmith.infra_provisioners.ansible_provisioner import AnsibleProvisioner
from opsmith.infra_provisioners.terraform_provisioner import TerraformProvisioner

#: The templates shipped with the package. The factory holds it so a provisioner does not have to
#: work out where it lives, and so a test can point the whole run at a different tree.
PACKAGE_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


class TerraformRunner(Protocol):
    """What a strategy actually uses a terraform provisioner for.

    Named as a protocol rather than the concrete class because the point of the factory is that
    a run can be handed something else - a recording double in the test suite today, and whatever
    a later phase needs to wrap a real one. The concrete
    :class:`~opsmith.infra_provisioners.terraform_provisioner.TerraformProvisioner` satisfies it
    without declaring that it does.
    """

    def copy_template(self, template_name: str, provider: str): ...

    def init_and_apply(
        self, variables: Mapping[str, Any], env_vars: Optional[Mapping[str, Any]] = None
    ): ...

    def destroy(
        self, variables: Mapping[str, Any], env_vars: Optional[Mapping[str, Any]] = None
    ): ...

    def get_output(self) -> Dict[str, Any]: ...


class AnsibleRunner(Protocol):
    """What a strategy actually uses an ansible provisioner for. See :class:`TerraformRunner`."""

    def copy_template(self, template_name: str, provider: str): ...

    def run_playbook(
        self,
        playbook_name: str,
        extra_vars: Mapping[str, Any],
        inventory: Optional[str] = None,
        user: Optional[str] = None,
    ) -> Dict[str, str]: ...


class Provisioners(Protocol):
    """Where a run's provisioners come from.

    This is the type a context and a strategy hold, so that handing a run doubles is a matter of
    passing a different object rather than of patching the module that builds the real ones.
    """

    def terraform(self, working_dir: Path, step: str = ...) -> TerraformRunner: ...

    def ansible(self, working_dir: Path, step: str = ...) -> AnsibleRunner: ...


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
