"""Where the model comes from, resolved once at the start of a run.

``--model`` and ``--api-key`` are still required values, but they no longer have to be typed: the
environment or ``.opsmith.conf.yml`` can supply them, so a harness never needs a secret on the
command line. Resolution happens here, in one place, rather than in a Typer callback, so the order
of the options on the command line does not matter and every failure is an
:class:`~opsmith.core.errors.OpsmithError` rather than a usage error.
"""

import os
from dataclasses import dataclass
from typing import Optional

from pydantic_ai import Agent

from opsmith.agent import AgentDeps, build_agent
from opsmith.core.errors import InvalidArgument
from opsmith.models import MODEL_REGISTRY, BaseAiModel
from opsmith.settings import settings

#: The environment variable that names the model, checked after the option and before the
#: settings file.
MODEL_ENV_VAR = "OPSMITH_MODEL"

#: Where the model name came from, kept for the error details and for anything that later wants
#: to tell the user why it is using this model.
SOURCE_OPTION = "--model"
SOURCE_ENVIRONMENT = MODEL_ENV_VAR
SOURCE_SETTINGS = ".opsmith.conf.yml"


@dataclass(frozen=True)
class ModelConfig:
    """A model that exists in the registry, and the key to call it with."""

    model: BaseAiModel
    api_key: str
    source: str


def _model_hint() -> str:
    """
    Builds the hint shown whenever the model name is missing or unusable.

    :return: A sentence naming every way to supply a model, followed by the known model names.
    """
    known = "\n  ".join(MODEL_REGISTRY.model_names)
    return (
        f"Pass --model, or set {MODEL_ENV_VAR}, or add 'model:' to {SOURCE_SETTINGS}."
        f" Known models:\n  {known}"
    )


def _resolve_model_name(model: Optional[str]) -> tuple[str, str]:
    """
    Finds the model name and says where it came from.

    :param model: The value of the --model option, if one was given.
    :return: The model name and the source it was read from.
    :raises InvalidArgument: No source supplied a model name.
    """
    if model:
        return model, SOURCE_OPTION

    from_environment = os.environ.get(MODEL_ENV_VAR)
    if from_environment:
        return from_environment, SOURCE_ENVIRONMENT

    if settings.model:
        return settings.model, SOURCE_SETTINGS

    raise InvalidArgument(
        "No model configured.",
        hint=_model_hint(),
        details={"models": MODEL_REGISTRY.model_names},
    )


def _resolve_api_key(model: BaseAiModel, api_key: Optional[str]) -> str:
    """
    Finds the API key for a model, from the option or the provider's environment variable.

    The settings file is deliberately not a source. It sits in the repository and nothing
    gitignores it, so a key written there is refused rather than used.

    :param model: The model the key is for.
    :param api_key: The value of the --api-key option, if one was given.
    :return: The API key.
    :raises InvalidArgument: A key was written into the settings file, or no source supplied one.
    """
    if settings.api_key:
        raise InvalidArgument(
            f"{SOURCE_SETTINGS} carries an api_key, which Opsmith does not read.",
            hint=(
                f"Remove it and pass --api-key, or set {model.api_key_env_var}. The file is"
                " part of the repository and is not a safe place for a secret."
            ),
            details={"env_var": model.api_key_env_var},
        )

    resolved = api_key or os.environ.get(model.api_key_env_var)
    if not resolved:
        raise InvalidArgument(
            f"No API key configured for {model.model_name_abs()}.",
            hint=f"Pass --api-key, or set {model.api_key_env_var}.",
            details={"model": model.model_name_abs(), "env_var": model.api_key_env_var},
        )
    return resolved


def resolve_model_config(model: Optional[str], api_key: Optional[str]) -> ModelConfig:
    """
    Resolves the model and its key from the options, the environment and the settings file.

    :param model: The value of the --model option, if one was given.
    :param api_key: The value of the --api-key option, if one was given.
    :return: The resolved configuration.
    :raises InvalidArgument: The model is missing or unknown, or there is no key for it.
    """
    name, source = _resolve_model_name(model)

    try:
        model_class = MODEL_REGISTRY.get_model_class(name)
    except ValueError:
        raise InvalidArgument(
            f"Unknown model: {name}.",
            hint=_model_hint(),
            details={"model": name, "source": source, "models": MODEL_REGISTRY.model_names},
        )

    resolved_model = model_class()
    return ModelConfig(
        model=resolved_model,
        api_key=_resolve_api_key(resolved_model, api_key),
        source=source,
    )


def configure_agent(config: ModelConfig, instrument: bool = False) -> Agent[AgentDeps, str]:
    """
    Exports the API key and builds the agent for the run.

    The key goes into the environment because that is where pydantic-ai reads it from when it
    resolves a "provider:name" model string.

    :param config: The resolved model configuration.
    :param instrument: Whether to instrument the agent for tracing.
    :return: The agent every model call in the run goes through.
    """
    config.model.ensure_auth(config.api_key)
    return build_agent(model_config=config.model, instrument=instrument)
