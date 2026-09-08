import re
import secrets
import shutil
import string
import subprocess
from typing import List


def get_missing_external_dependencies(dependencies: List[str]) -> List[str]:
    """
    Checks if a list of external command-line tools are installed and operational.
    For Docker, it checks if the daemon is running. For Terraform, it checks if it's executable.

    :param dependencies: A list of command names to check (e.g., ['docker', 'terraform']).
    :return: A list of dependency names that were not found or are not operational.
    """
    missing_deps = []
    for dep in dependencies:
        command = None
        if dep == "docker":
            command = ["docker", "info"]
        elif dep == "terraform":
            command = ["terraform", "version"]

        if command:
            try:
                subprocess.run(command, check=True, capture_output=True)
            except (subprocess.CalledProcessError, FileNotFoundError):
                missing_deps.append(dep)
        else:
            if not shutil.which(dep):
                missing_deps.append(dep)
    return missing_deps


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
