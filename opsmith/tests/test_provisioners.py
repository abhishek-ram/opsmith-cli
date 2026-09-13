"""Tests for the provisioners: how they report, and how they fail.

These are the only modules that shell out, so they are where a subprocess failure becomes an
error the CLI can map to an exit code, and where a template directory is looked up.
"""

import json
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opsmith.core.errors import EXIT_CODES, AnsibleFailed, TerraformFailed
from opsmith.core.provisioners import PACKAGE_TEMPLATES_DIR, ProvisionerFactory
from opsmith.infra_provisioners.ansible_provisioner import AnsibleProvisioner


@pytest.fixture
def templates_dir(tmp_path: Path) -> Path:
    """
    A template tree with one lower-case provider directory, as the packaged one has.

    :param tmp_path: pytest's per-test temporary directory.
    :return: The root of the tree.
    """
    template = tmp_path / "templates" / "virtual_machine" / "aws"
    template.mkdir(parents=True)
    (template / "main.tf").write_text('resource "null_resource" "vm" {}\n')
    return tmp_path / "templates"


@pytest.fixture
def factory(events, templates_dir: Path) -> ProvisionerFactory:
    """A factory wired to the recording sink and the test template tree."""
    return ProvisionerFactory(events=events, templates_dir=templates_dir)


def test_the_factory_defaults_to_the_packaged_templates(events):
    """Nothing has to know where the templates live; the factory does."""
    factory = ProvisionerFactory(events=events)

    assert factory.templates_dir == PACKAGE_TEMPLATES_DIR
    assert (factory.templates_dir / "virtual_machine" / "aws").is_dir()


def test_the_factory_creates_the_working_directory(factory, tmp_path: Path):
    """A provisioner is usable the moment it is built, which destroy() also relies on."""
    working_dir = tmp_path / "environments" / "prod" / "virtual_machine"

    factory.terraform(working_dir)

    assert working_dir.is_dir()


def test_copy_template_normalises_the_provider_casing(factory, tmp_path: Path):
    """
    Call sites pass the provider's own casing - "AWS" from ``name()``, "aws" elsewhere - but the
    directories on disk are lower-case. Normalising in one place is what stops the lookup failing
    on a case-sensitive filesystem, where it worked on the author's Mac.
    """
    working_dir = tmp_path / "run"
    provisioner = factory.terraform(working_dir)

    provisioner.copy_template("virtual_machine", "AWS")

    assert (working_dir / "main.tf").exists()


def test_copy_template_reports_a_missing_template_as_a_mapped_error(factory, tmp_path: Path):
    """
    A provider without a template for a step used to raise FileNotFoundError, which the CLI could
    only report as INTERNAL. It names the directory it looked in now, and exits 4.
    """
    provisioner = factory.ansible(tmp_path / "run")

    with pytest.raises(AnsibleFailed) as raised:
        provisioner.copy_template("virtual_machine", "azure")

    assert EXIT_CODES[raised.value.code] == 4
    assert raised.value.details["template"] == "virtual_machine"
    assert "azure" in raised.value.details["template_dir"]


def test_copy_template_reports_the_files_it_copied(factory, events, tmp_path: Path):
    """Copying a template is progress, and progress is an event rather than a print."""
    factory.terraform(tmp_path / "run", step="vm").copy_template("virtual_machine", "aws")

    assert any("files copied to" in message for message in events.messages())
    assert {event.step for event in events.events} == {"vm"}


def test_a_command_streams_every_line_as_an_output_event(factory, events, tmp_path: Path):
    """
    Subprocess output is one event per line, which is what lets it render as grey terminal text
    or as NDJSON on stderr without the provisioner knowing which.
    """
    provisioner = factory.terraform(tmp_path / "run", step="vm")

    provisioner._run_command(["python3", "-c", "print('one'); print('two')"])

    assert events.messages(kind="output") == ["one", "two"]


def test_a_command_collects_the_values_a_playbook_hands_back(factory, tmp_path: Path):
    """
    Playbooks return values by printing a marker. Parsing it out of the stream is how the caller
    gets an image URL or a set of fetched files back.
    """
    provisioner = factory.ansible(tmp_path / "run", step="build")
    marker = '{"msg": "OPSMITH_OUTPUT_IMAGE_URL=registry.test/app:latest"}'

    outputs = provisioner._run_command(["python3", "-c", f"print({marker!r})"])

    assert outputs == {"image_url": "registry.test/app:latest"}


def test_a_failed_command_carries_the_command_and_the_output_tail(factory, tmp_path: Path):
    """
    A non-zero exit is a TerraformFailed with what ran and what it said, so a harness reading the
    JSON envelope can see the failure without the terminal scrollback.
    """
    provisioner = factory.terraform(tmp_path / "run", step="vm")

    with pytest.raises(TerraformFailed) as raised:
        provisioner._run_command(
            ["python3", "-c", "import sys; print('Error: quota exceeded'); sys.exit(3)"]
        )

    assert EXIT_CODES[raised.value.code] == 4
    assert raised.value.details["returncode"] == 3
    assert "python3" in raised.value.details["command"]
    assert "Error: quota exceeded" in raised.value.details["output_tail"]


def test_a_missing_executable_is_reported_with_the_tool_to_install(factory, tmp_path: Path):
    """
    A missing terraform binary used to surface as a bare FileNotFoundError. It names the tool and
    exits 4 now, so the message is actionable.
    """
    provisioner = factory.terraform(tmp_path / "run")

    with pytest.raises(TerraformFailed) as raised:
        provisioner._run_command(["terraform-that-does-not-exist", "apply"])

    assert raised.value.details["executable"] == "terraform"
    assert "PATH" in raised.value.hint


def test_terraform_outputs_are_read_back_as_values(factory, tmp_path: Path):
    """Terraform wraps each output in a value object; the caller wants the values."""
    provisioner = factory.terraform(tmp_path / "run")
    completed = MagicMock(stdout=json.dumps({"registry_url": {"value": "registry.test/app"}}))

    with patch("subprocess.run", return_value=completed):
        assert provisioner.get_output() == {"registry_url": "registry.test/app"}


def test_unreadable_terraform_outputs_are_reported_as_a_failure(factory, tmp_path: Path):
    """Terraform writing something that is not JSON is a failure, not a crash."""
    provisioner = factory.terraform(tmp_path / "run")

    with patch("subprocess.run", return_value=MagicMock(stdout="not json at all")):
        with pytest.raises(TerraformFailed) as raised:
            provisioner.get_output()

    assert "JSON" in raised.value.message


def test_failing_to_read_terraform_outputs_carries_the_stderr(factory, tmp_path: Path):
    """When the output command itself fails, what terraform said travels with the error."""
    provisioner = factory.terraform(tmp_path / "run")
    failure = subprocess.CalledProcessError(1, "terraform output", stderr="no state file")

    with patch("subprocess.run", side_effect=failure):
        with pytest.raises(TerraformFailed) as raised:
            provisioner.get_output()

    assert raised.value.details["output_tail"] == "no state file"


def test_a_missing_playbook_is_reported_as_a_mapped_error(factory, tmp_path: Path):
    """
    A template that did not ship the playbook it should have is an AnsibleFailed naming the file,
    rather than a FileNotFoundError the CLI can only call INTERNAL.
    """
    provisioner = factory.ansible(tmp_path / "run")

    with pytest.raises(AnsibleFailed) as raised:
        provisioner.run_playbook("main.yml", extra_vars={})

    assert EXIT_CODES[raised.value.code] == 4
    assert raised.value.details["playbook"].endswith("main.yml")


def test_the_ansible_provisioner_writes_its_config(factory, tmp_path: Path):
    """The generated ansible.cfg is what turns off host key checking for a fresh VM."""
    working_dir = tmp_path / "run"

    factory.ansible(working_dir)

    assert "host_key_checking = False" in (working_dir / "ansible.cfg").read_text()


def test_playbook_variables_never_reach_the_process_table(tmp_path: Path, events):
    """
    The extra vars carry the whole compose .env. Anything on a command line is readable by every
    process on the machine and is repeated in the details of a failure, so they travel in a file
    that only this user can read - and that is gone once the playbook has run.
    """
    working_dir = tmp_path / "compose"
    provisioner = AnsibleProvisioner(
        working_dir=working_dir, events=events, step="compose", templates_dir=tmp_path
    )
    (working_dir / "main.yml").write_text("- hosts: all\n", encoding="utf-8")

    seen = {}

    def capture(command, env=None):
        """Reads the vars file while the playbook would still have been running."""
        seen["command"] = command
        path = Path(command[command.index("--extra-vars") + 1].lstrip("@"))
        seen["path"] = path
        seen["contents"] = json.loads(path.read_text(encoding="utf-8"))
        seen["mode"] = stat.S_IMODE(os.stat(path).st_mode)
        return {}

    with patch.object(AnsibleProvisioner, "_run_command", side_effect=capture):
        provisioner.run_playbook("main.yml", extra_vars={"env_file_content": "SECRET=hunter2"})

    assert "hunter2" not in " ".join(seen["command"])
    assert seen["contents"] == {"env_file_content": "SECRET=hunter2"}
    assert seen["mode"] == stat.S_IRUSR | stat.S_IWUSR
    assert not seen["path"].exists()
