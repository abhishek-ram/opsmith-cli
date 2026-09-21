"""Tests for how the model and its API key are resolved, in opsmith/core/llm.py."""

import os

import pytest

from opsmith.core import llm
from opsmith.core.errors import InvalidArgument
from opsmith.core.llm import MODEL_ENV_VAR, configure_agent, resolve_model_config
from opsmith.models import MODEL_REGISTRY
from opsmith.tests.conftest import hint_of

FIRST_MODEL = MODEL_REGISTRY.model_names[0]
SECOND_MODEL = MODEL_REGISTRY.model_names[1]


def _key_variable(model_name: str) -> str:
    """
    Returns the environment variable a model reads its API key from.

    :param model_name: The absolute model name, as the registry keys it.
    :return: The name of the provider's API key variable.
    """
    return MODEL_REGISTRY.get_model_class(model_name)().api_key_env_var


@pytest.fixture(autouse=True)
def clean_model_environment(monkeypatch):
    """
    Clears everything the resolution order reads, so a test only sees what it sets itself.

    The developer running the suite very likely has a provider key exported, which would
    otherwise satisfy a test that is checking a key is missing.
    """
    monkeypatch.delenv(MODEL_ENV_VAR, raising=False)
    for model_name in MODEL_REGISTRY.model_names:
        monkeypatch.delenv(_key_variable(model_name), raising=False)
    monkeypatch.setattr(llm.settings, "model", None)
    monkeypatch.setattr(llm.settings, "api_key", None)


def test_the_option_wins_over_the_environment_and_the_settings_file(monkeypatch):
    """--model beats both OPSMITH_MODEL and the model named in .opsmith.conf.yml."""
    monkeypatch.setenv(MODEL_ENV_VAR, SECOND_MODEL)
    monkeypatch.setattr(llm.settings, "model", SECOND_MODEL)

    config = resolve_model_config(FIRST_MODEL, "a-key")

    assert config.model.model_name_abs() == FIRST_MODEL
    assert config.source == "--model"


def test_the_environment_wins_over_the_settings_file(monkeypatch):
    """With no --model, OPSMITH_MODEL beats the settings file."""
    monkeypatch.setenv(MODEL_ENV_VAR, FIRST_MODEL)
    monkeypatch.setattr(llm.settings, "model", SECOND_MODEL)

    config = resolve_model_config(None, "a-key")

    assert config.model.model_name_abs() == FIRST_MODEL
    assert config.source == MODEL_ENV_VAR


def test_the_settings_file_is_the_last_source(monkeypatch):
    """With neither the option nor the variable set, the settings file supplies the model."""
    monkeypatch.setattr(llm.settings, "model", FIRST_MODEL)

    config = resolve_model_config(None, "a-key")

    assert config.model.model_name_abs() == FIRST_MODEL
    assert config.source == ".opsmith.conf.yml"


def test_no_model_anywhere_is_an_invalid_argument():
    """With no source at all the run stops with the registry names in the hint."""
    with pytest.raises(InvalidArgument) as raised:
        resolve_model_config(None, "a-key")

    error = raised.value
    assert error.code == "INVALID_ARGUMENT"
    assert MODEL_ENV_VAR in hint_of(error)
    assert all(name in hint_of(error) for name in MODEL_REGISTRY.model_names)
    assert error.details["models"] == MODEL_REGISTRY.model_names


def test_an_unknown_model_is_an_invalid_argument():
    """A name the registry does not know is a usage error, not a crash, and lists the names."""
    with pytest.raises(InvalidArgument) as raised:
        resolve_model_config("nope:nope", "a-key")

    error = raised.value
    assert error.code == "INVALID_ARGUMENT"
    assert "nope:nope" in error.message
    assert all(name in hint_of(error) for name in MODEL_REGISTRY.model_names)
    assert error.details["model"] == "nope:nope"


def test_the_key_option_wins_over_the_provider_variable(monkeypatch):
    """--api-key beats the provider's own environment variable."""
    monkeypatch.setenv(_key_variable(FIRST_MODEL), "from-the-environment")

    config = resolve_model_config(FIRST_MODEL, "from-the-option")

    assert config.api_key == "from-the-option"


def test_the_key_falls_back_to_the_provider_variable(monkeypatch):
    """With no --api-key the provider's own variable supplies it, which is what lets a
    headless run keep the secret off the command line."""
    monkeypatch.setenv(_key_variable(FIRST_MODEL), "from-the-environment")

    config = resolve_model_config(FIRST_MODEL, None)

    assert config.api_key == "from-the-environment"


def test_no_key_anywhere_names_the_variable_in_the_hint():
    """A model with no key stops the run with the variable to set."""
    with pytest.raises(InvalidArgument) as raised:
        resolve_model_config(FIRST_MODEL, None)

    error = raised.value
    assert error.code == "INVALID_ARGUMENT"
    assert _key_variable(FIRST_MODEL) in hint_of(error)
    assert error.details["env_var"] == _key_variable(FIRST_MODEL)


def test_a_key_in_the_settings_file_is_refused(monkeypatch):
    """The settings file is committed, so a key written there is refused rather than used."""
    monkeypatch.setattr(llm.settings, "api_key", "committed-secret")

    with pytest.raises(InvalidArgument) as raised:
        resolve_model_config(FIRST_MODEL, None)

    assert ".opsmith.conf.yml" in raised.value.message
    assert _key_variable(FIRST_MODEL) in hint_of(raised.value)


def test_configuring_the_agent_exports_the_key(monkeypatch):
    """
    pydantic-ai reads the key from the environment when it resolves a "provider:name" model,
    so building the agent is what puts it there.
    """
    variable = _key_variable(FIRST_MODEL)
    monkeypatch.setenv(variable, "placeholder")
    config = resolve_model_config(FIRST_MODEL, "the-real-key")

    agent = configure_agent(config)

    assert os.environ[variable] == "the-real-key"
    assert agent is not None
