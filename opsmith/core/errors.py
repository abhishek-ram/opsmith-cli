"""The single error hierarchy for Opsmith and the code-to-exit-code map.

Every failure that Opsmith can describe is an :class:`OpsmithError` carrying a stable
``code``. The CLI turns that code into an error envelope and a process exit code through
:data:`EXIT_CODES`. Anything that is not an :class:`OpsmithError` is reported as ``INTERNAL``
with exit code 1.
"""

from typing import Any, ClassVar, Dict, List, Optional


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


class MissingAnswerError(OpsmithError):
    """A run with nobody at the keyboard reached a question it has no answer for.

    It shares its exit code with :class:`InteractionCancelled` because it means the same thing to
    whoever runs the command again: supply the answer and re-run. Everything a driver needs in
    order to do that is in ``details`` - which key went unanswered, what shape the answer takes,
    the choices it has to come from, and the environment variable that carries it when it is a
    secret that should not go on a command line.
    """

    code: ClassVar[str] = "MISSING_ANSWER"

    def __init__(
        self,
        key: str,
        question: str,
        *,
        primitive: str,
        choices: Optional[List[Dict[str, Any]]] = None,
        default: Any = None,
        secret: bool = False,
        env_var: Optional[str] = None,
        resume: Optional[str] = None,
    ):
        """
        :param key: The interaction key that went unanswered.
        :param question: The question as it was worded, so the report says what was missing.
        :param primitive: Which primitive asked, so a driver knows what shape to supply.
        :param choices: The options the answer must come from, as label and value pairs, when the
            question offered any.
        :param default: What the question would have used under ``--accept-defaults``.
        :param secret: Whether the answer must not be echoed, stored or put on a command line.
        :param env_var: The environment variable this key reads, which is how a secret is supplied.
        :param resume: The command to run again once the answer is available.
        """
        self.key = key
        super().__init__(
            message=f"No answer for '{key}': {question}",
            hint=_missing_answer_hint(key, secret=secret, env_var=env_var, resume=resume),
            details={
                "key": key,
                "question": question,
                "primitive": primitive,
                "choices": choices or [],
                "default": default,
                "secret": secret,
                "env_var": env_var,
                "resume": resume,
            },
        )


def _missing_answer_hint(
    key: str, *, secret: bool, env_var: Optional[str], resume: Optional[str]
) -> str:
    """
    Writes the one sentence that tells a person how to answer the question that stopped the run.

    The hint carries the resume command as well as the flag, because the text renderer shows only
    the message and the hint; a driver reading JSON gets both from ``details`` either way.

    :param key: The interaction key that went unanswered.
    :param secret: Whether the value must not go on a command line.
    :param env_var: The environment variable this key reads.
    :param resume: The command to run again.
    :return: The hint.
    """
    if secret:
        supply = f"Set {env_var} or pass the value in --env-file" if env_var else "Use --env-file"
    else:
        supply = f"Supply it with --answer {key}=<value>"

    if resume:
        return f"{supply}, then run again: {resume}"
    return f"{supply}, then run the command again."


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


class ManualEditRequired(OpsmithError):
    """A document failed validation, the model could not fix it, and nobody can be asked to.

    This is the headless half of a fix editor. On a terminal the user is handed the document; with
    nobody there the proposal is written to its file and the run stops, so the fix is a matter of
    editing that file and running the same command again.
    """

    code: ClassVar[str] = "EDIT_REQUIRED"

    def __init__(self, key: str, message: str, *, path: str, resume: Optional[str] = None):
        """
        :param key: The interaction key of the editor that could not be opened.
        :param message: What the user would have been asked to fix, in plain text.
        :param path: The file holding the proposal, which is where the fix belongs.
        :param resume: The command that validates the fix, which is the same one that stopped.
        """
        self.key = key
        super().__init__(
            message=f"{message} Nothing could be fixed automatically, and there is no editor.",
            hint=(
                f"Edit {path}, then run again: {resume}"
                if resume
                else f"Edit {path}, then run the command again."
            ),
            details={"key": key, "path": path, "validate": resume, "resume": resume},
        )


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


# --- exit code 8: something outside opsmith has to happen first ----------------------------


class PendingActionError(OpsmithError):
    """The run is waiting on something only a person can do, such as creating a DNS record.

    A terminal waits and re-checks. A headless run polls until its timeout and then stops here,
    so the driver can perform the action and run the same command again, which resumes at the
    wait and finds it satisfied.
    """

    code: ClassVar[str] = "PENDING_ACTION"

    def __init__(
        self,
        key: str,
        message: str,
        *,
        details: Any = None,
        waited_s: Optional[int] = None,
        timeout_s: Optional[int] = None,
        resume: Optional[str] = None,
    ):
        """
        :param key: The interaction key this wait is recorded under.
        :param message: What has to happen, in plain text.
        :param details: What has to be done, such as the DNS records that are still missing.
        :param waited_s: How long the run actually polled for.
        :param timeout_s: The limit it gave up at.
        :param resume: The command to run again once the action is done.
        """
        self.key = key
        payload = {
            "key": key,
            "action": message,
            "waited_s": waited_s,
            "timeout_s": timeout_s,
            "resume": resume,
        }
        if isinstance(details, dict):
            payload.update(details)
        elif details is not None:
            payload["details"] = details

        super().__init__(
            message=f"Still waiting at '{key}': {message}",
            hint=(
                f"Do it, then run again: {resume}"
                if resume
                else "Do it, then run the command again."
            ),
            details=payload,
        )


EXIT_CODES: Dict[str, int] = {
    "INTERNAL": 1,
    "INVALID_CONFIG": 2,
    "INVALID_ARGUMENT": 2,
    "UNKNOWN_ENVIRONMENT": 2,
    "UNKNOWN_SERVICE": 2,
    "INTERACTION_CANCELLED": 3,
    "MISSING_ANSWER": 3,
    "TERRAFORM_FAILED": 4,
    "ANSIBLE_FAILED": 4,
    "DOCKER_FAILED": 4,
    "DEPLOY_UNHEALTHY": 4,
    "EDIT_REQUIRED": 4,
    "CLOUD_CREDENTIALS": 5,
    "CLOUD_PERMISSION": 5,
    "LLM_GAVE_UP": 6,
    "PENDING_ACTION": 8,
}
"""Maps every declared error code to the process exit code the CLI returns for it.

EDIT_REQUIRED sits in the external-tool band because that is where the migration plan puts a fix
editor that cannot be opened, even though the band is otherwise about terraform, ansible and
docker: a document that will not validate and cannot be edited is the same kind of stop. Codes
owned by later phases (UNKNOWN_RECIPE, TEMPLATE_*, CAPACITY_UNSATISFIABLE, STATE_*) are not
declared yet.
"""
