import hashlib
import json
import re
import secrets
import shutil
import string
import subprocess
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import dns.resolver

from opsmith.settings import settings

#: What the distribution is called on PyPI, which is not what the package is called on disk.
DISTRIBUTION_NAME = "opsmith-cli"


def package_version() -> str:
    """
    Returns the version of Opsmith that is running.

    Read from the installed distribution rather than from ``pyproject.toml``, because a wheel
    carries no ``pyproject.toml``. A checkout that has never been installed has no version to
    report, which is the one case this answers "unknown" for.

    :return: The version, as a string.
    """
    try:
        return version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return "unknown"


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
        # unknown, and phase 4 is what will care about the difference.
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


def resolve_dns_record(record_type: str, name: str) -> List[str]:
    """
    Asks the resolver what a name currently holds.

    :param record_type: The record type to look up, such as "A" or "CNAME".
    :param name: The name to look up.
    :return: What the name resolves to, normalised for comparison, or nothing at all when it
        resolves to nothing or the resolver could not answer.
    """
    try:
        answers = dns.resolver.resolve(name, record_type)
    except Exception:
        # Every outcome that is not an answer means the same thing to a caller waiting for a
        # record to appear: not yet. That includes the resolver being unreachable, which must
        # never be the reason a deployment fails.
        return []

    return [_normalise_dns_value(str(answer)) for answer in answers]


def dns_record_is_published(record: Dict[str, str]) -> bool:
    """
    Reports whether a DNS record Opsmith asked for has actually been created.

    :param record: The record, with its ``type``, ``name`` and ``value``.
    :return: Whether the name now resolves to the value Opsmith asked for.
    """
    record_type = (record.get("type") or "").upper()
    name = record.get("name") or ""
    value = record.get("value") or ""
    if not record_type or not name or not value:
        return False

    published = resolve_dns_record(record_type, name)
    return _normalise_dns_value(value) in published


def _normalise_dns_value(value: str) -> str:
    """
    Puts a record value in the one form two of them can be compared in.

    A resolver reports a name fully qualified and a provider usually does not, and neither is
    case sensitive, so "d1234.cloudfront.net." and "D1234.cloudfront.net" are one value.

    :param value: The value as it was written or as it was resolved.
    :return: The value, lower-cased and without a trailing dot.
    """
    return value.strip().rstrip(".").lower()


def project_state_dir(deployments_path: Path) -> Path:
    """
    Returns where Opsmith keeps what it remembers about one project.

    It is outside the repository. What lives there is not authored by anybody - answers already
    given, the secrets an environment needs before it exists, which steps have finished - and a
    repository holds what a person wrote plus the state of what is deployed. Keeping secrets out
    of the tree is the part that matters most: an ignore rule is advice, and does not survive
    ``git add -f``, an archive of the directory, or a build context that never read it.

    The directory is named after the project and a digest of where it lives, so two checkouts of
    the same project share what they remember and two projects of the same name do not. Moving a
    project means being asked its questions once more, which is the cheapest failure available.

    :param deployments_path: The project's ``.opsmith`` directory.
    :return: The directory to keep this project's state in.
    """
    resolved = deployments_path.expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    name = slugify(resolved.parent.name) or "project"

    return Path(settings.state_dir).expanduser() / "projects" / f"{name}-{digest}"
