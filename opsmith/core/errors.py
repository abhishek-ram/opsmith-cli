"""The single error hierarchy for Opsmith and the code-to-exit-code map.

Every failure that Opsmith can describe is an :class:`OpsmithError` carrying a stable
``code``. The CLI turns that code into an error envelope and a process exit code through
:data:`EXIT_CODES`. Anything that is not an :class:`OpsmithError` is reported as ``INTERNAL``
with exit code 1.
"""

from typing import ClassVar, Dict, Optional


class OpsmithError(Exception):
    """Base class for every error Opsmith raises deliberately.

    Subclasses override :attr:`code` only; the constructor is the same for all of them, so
    ``raise InvalidConfig("...")`` is the whole call. The four attributes are exactly the
    fields the JSON error envelope needs.
    """

    code: ClassVar[str] = "INTERNAL"

    def __init__(
        self,
        message: str,
        hint: Optional[str] = None,
        details: Optional[Dict] = None,
    ):
        """
        :param message: What went wrong, in one sentence, for a human to read.
        :param hint: What the user can do about it, if there is a useful answer.
        :param details: Machine-readable context for a harness, such as the offending path.
        """
        self.message = message
        self.hint = hint
        self.details = details if details is not None else {}
        super().__init__(message)


# --- exit code 2: usage or validation errors ---------------------------------------------


class InvalidConfig(OpsmithError):
    """The deployment configuration is missing, unparsable or fails validation."""

    code: ClassVar[str] = "INVALID_CONFIG"


class InvalidArgument(OpsmithError):
    """An argument, option or environment variable holds a value Opsmith cannot use."""

    code: ClassVar[str] = "INVALID_ARGUMENT"


class NotAGitRepository(InvalidArgument):
    """The source directory is not inside a git repository."""


class UnknownEnvironment(OpsmithError):
    """The named deployment environment does not exist or has never been deployed."""

    code: ClassVar[str] = "UNKNOWN_ENVIRONMENT"


class UnknownService(OpsmithError):
    """The named service is not present in the deployment configuration."""

    code: ClassVar[str] = "UNKNOWN_SERVICE"


# --- exit code 3: the run needs an answer it does not have --------------------------------


class InteractionCancelled(OpsmithError):
    """A person was asked something and declined to answer.

    It shares its exit code with the missing answer a headless run reports, because it means
    the same thing to whoever runs the command again: supply the answer and re-run.
    """

    code: ClassVar[str] = "INTERACTION_CANCELLED"

    def __init__(self, key: str, message: str):
        """
        :param key: The interaction key that went unanswered.
        :param message: The question that was asked, so the report says what was cancelled.
        """
        self.key = key
        super().__init__(
            message=f"Cancelled at '{key}': {message}",
            hint="Run the command again and answer the question, or supply the answer up front.",
            details={"key": key, "question": message},
        )


# --- exit code 4: an external tool failed ------------------------------------------------


class TerraformFailed(OpsmithError):
    """A terraform command exited non-zero."""

    code: ClassVar[str] = "TERRAFORM_FAILED"


class AnsibleFailed(OpsmithError):
    """An ansible playbook exited non-zero."""

    code: ClassVar[str] = "ANSIBLE_FAILED"


class DockerFailed(OpsmithError):
    """A docker command exited non-zero."""

    code: ClassVar[str] = "DOCKER_FAILED"


class DeployUnhealthy(OpsmithError):
    """The deployment completed but the application did not come up healthy."""

    code: ClassVar[str] = "DEPLOY_UNHEALTHY"


# --- exit code 5: cloud credentials and permissions --------------------------------------


class CloudCredentialsError(OpsmithError):
    """The cloud provider's credentials are missing, expired or unusable.

    This class keeps a two-argument constructor rather than the base one because it predates
    the hierarchy and is raised from six sites in the AWS and GCP providers.
    """

    code: ClassVar[str] = "CLOUD_CREDENTIALS"

    def __init__(self, message: str, help_url: str):
        """
        :param message: What failed while reading or using the credentials.
        :param help_url: Provider documentation explaining how to set the credentials up.
        """
        self.help_url = help_url
        super().__init__(
            message=message,
            hint=(
                "Please ensure your credentials are set up correctly. For more information,"
                f" visit: {help_url}"
            ),
            details={"help_url": help_url},
        )


class CloudPermission(OpsmithError):
    """The cloud credentials are valid but lack a permission the operation needs."""

    code: ClassVar[str] = "CLOUD_PERMISSION"


# --- exit code 6: the model could not produce a usable result ----------------------------


class LlmGaveUp(OpsmithError):
    """A model step could not produce a usable result within its configured limits."""

    code: ClassVar[str] = "LLM_GAVE_UP"


EXIT_CODES: Dict[str, int] = {
    "INTERNAL": 1,
    "INVALID_CONFIG": 2,
    "INVALID_ARGUMENT": 2,
    "UNKNOWN_ENVIRONMENT": 2,
    "UNKNOWN_SERVICE": 2,
    "INTERACTION_CANCELLED": 3,
    "TERRAFORM_FAILED": 4,
    "ANSIBLE_FAILED": 4,
    "DOCKER_FAILED": 4,
    "DEPLOY_UNHEALTHY": 4,
    "CLOUD_CREDENTIALS": 5,
    "CLOUD_PERMISSION": 5,
    "LLM_GAVE_UP": 6,
}
"""Maps every declared error code to the process exit code the CLI returns for it.

MISSING_ANSWER, which shares exit code 3 with INTERACTION_CANCELLED, and PENDING_ACTION
(exit 8) arrive with headless mode in part 0e. Codes owned by later phases (UNKNOWN_RECIPE,
TEMPLATE_*, CAPACITY_UNSATISFIABLE, STATE_*) are not declared yet.
"""
