"""Runs terraform in a working directory prepared from a template."""

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from opsmith.core.errors import TerraformFailed
from opsmith.core.events import EventSink
from opsmith.infra_provisioners.base_provisioner import BaseInfrastructureProvisioner


class TerraformProvisioner(BaseInfrastructureProvisioner):
    """A wrapper for running terraform commands."""

    def __init__(self, working_dir: Path, events: EventSink, step: str, templates_dir: Path):
        """
        :param working_dir: Directory the terraform configuration lives and runs in.
        :param events: Sink the streamed output is reported to.
        :param step: The step name every event from this provisioner carries.
        :param templates_dir: Root of the template tree to copy configurations from.
        """
        super().__init__(
            working_dir=working_dir,
            command_name="Terraform",
            executable="terraform",
            events=events,
            step=step,
            templates_dir=templates_dir,
            error_class=TerraformFailed,
        )

    @staticmethod
    def _build_vars(
        variables: Mapping[str, Any], env_vars: Optional[Mapping[str, Any]] = None
    ) -> tuple[list, dict]:
        vars_list = []
        for key, value in variables.items():
            vars_list.extend(["-var", f"{key}={value}"])

        tf_env_vars = {}
        for key, value in (env_vars or {}).items():
            tf_env_vars[f"TF_VAR_{key}"] = str(value)

        return vars_list, tf_env_vars

    def init_and_apply(
        self, variables: Mapping[str, Any], env_vars: Optional[Mapping[str, Any]] = None
    ):
        """
        Initializes and applies the terraform configuration.
        """
        self._run_command(["terraform", "init", "-no-color"])
        command = ["terraform", "apply", "-auto-approve", "-no-color"]
        vars_list, tf_env_vars = self._build_vars(variables, env_vars)
        command.extend(vars_list)

        self._run_command(command, env=tf_env_vars)

    def destroy(self, variables: Mapping[str, Any], env_vars: Optional[Mapping[str, Any]] = None):
        """
        Destroys the terraform-managed infrastructure.
        """
        command = ["terraform", "destroy", "-auto-approve", "-no-color"]
        vars_list, tf_env_vars = self._build_vars(variables, env_vars)
        command.extend(vars_list)
        self._run_command(command, env=tf_env_vars)

    def get_output(self) -> Dict[str, Any]:
        """
        Retrieves terraform outputs from the working directory.

        :return: The output names mapped to their values.
        :raises TerraformFailed: terraform is missing, failed, or wrote something that is not JSON.
        """
        try:
            result = subprocess.run(
                ["terraform", "output", "-json"],
                cwd=self.working_dir,
                capture_output=True,
                text=True,
                check=True,
                encoding="utf-8",
            )
        except FileNotFoundError:
            raise TerraformFailed(
                "'terraform' command not found.",
                hint="Install Terraform and make sure 'terraform' is on your PATH.",
                details={"executable": "terraform"},
            )
        except subprocess.CalledProcessError as err:
            raise TerraformFailed(
                f"Failed to read Terraform outputs. Exit code: {err.returncode}",
                hint=f"Re-run 'terraform output -json' in {self.working_dir} to see the failure.",
                details={
                    "command": "terraform output -json",
                    "working_dir": str(self.working_dir),
                    "returncode": err.returncode,
                    "output_tail": (err.stderr or "").strip(),
                },
            )

        try:
            outputs = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise TerraformFailed(
                "Failed to parse Terraform outputs as JSON.",
                details={"working_dir": str(self.working_dir), "output_tail": result.stdout[-500:]},
            )

        return {key: value["value"] for key, value in outputs.items()}
