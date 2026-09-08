import json
import re
import secrets
import shutil
import string
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence


@dataclass
class ExternalToolReport:
    """What a probe of the external command line tools found."""

    #: The tools that are absent, or present but not working.
    missing: List[str] = field(default_factory=list)

    #: The versions of the tools that report one. Only terraform does today.
    versions: Dict[str, str] = field(default_factory=dict)


def _probe_terraform() -> Optional[str]:
    """
    Runs terraform and reads its version out of the JSON it prints.

    :return: The version terraform reports, an empty string when it runs but says nothing this
        function understands, or None when it is missing or broken.
    """
    try:
        completed = subprocess.run(
            ["terraform", "version", "-json"], check=True, capture_output=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    try:
        return json.loads(completed.stdout)["terraform_version"]
    except (ValueError, KeyError, TypeError):
        # A terraform too old for -json still counts as installed; the version is what is
        # unknown, and phase 3 is what will care about the difference.
        return ""


def _probe_docker() -> bool:
    """
    Checks that the docker daemon answers, not merely that the client is installed.

    :return: Whether docker is usable.
    """
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def check_external_tools(tools: Sequence[str]) -> ExternalToolReport:
    """
    Checks that a set of external command line tools are installed and operational.

    :param tools: The command names to check (e.g. ['docker', 'terraform']).
    :return: The tools that were missing, and the versions of those that report one.
    """
    report = ExternalToolReport()
    for tool in tools:
        if tool == "docker":
            if not _probe_docker():
                report.missing.append(tool)
        elif tool == "terraform":
            version = _probe_terraform()
            if version is None:
                report.missing.append(tool)
            elif version:
                report.versions[tool] = version
        elif not shutil.which(tool):
            report.missing.append(tool)
    return report


def slugify(value: str) -> str:
    """
    Converts a given string into a slug format. The slug format is typically
    used for URLs, where spaces are replaced with hyphens and all characters
    are converted to lowercase.

    :param value: The string to be converted into slug format.
    :type value: str
    :return: A slugified version of the input string.
    :rtype: str
    """
    return re.sub(r"[^a-z0-9-]", "", value.lower().replace(" ", "-"))


def generate_secret_string(length: int = 32) -> str:
    """
    Generates a secure random string.

    :param length: The length of the secret string to generate.
    :return: A secure random string.
    """
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))
