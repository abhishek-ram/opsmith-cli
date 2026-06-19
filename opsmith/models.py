import abc
import os
from importlib.metadata import entry_points
from typing import Dict, List, Optional, Tuple, Type

from google.genai.types import ThinkingConfigDict, ThinkingLevel
from pydantic_ai.models.anthropic import AnthropicModelSettings
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.models.openai import (
    OpenAIChatModelSettings,
    OpenAIResponsesModelSettings,
)
from pydantic_ai.settings import ModelSettings
from rich import print


class BaseAiModel(abc.ABC):
    """Abstract base class for AI models."""

    @classmethod
    @abc.abstractmethod
    def name(cls) -> str:
        """The name of the model (e.g., 'gpt-4.1')."""
        raise NotImplementedError

    @classmethod
    @abc.abstractmethod
    def provider(cls) -> str:
        """The provider of the model (e.g., 'openai')."""
        raise NotImplementedError

    @classmethod
    @abc.abstractmethod
    def api_key_prefix(cls) -> str:
        """The prefix for the API key environment variable (e.g., 'OPENAI')."""
        raise NotImplementedError

    @classmethod
    @abc.abstractmethod
    def get_model_settings(cls) -> ModelSettings:
        """Returns model-specific settings."""
        raise NotImplementedError

    @classmethod
    def model_name_abs(cls) -> str:
        """The absolute model name used by pydantic-ai"""
        return f"{cls.provider()}:{cls.name()}"

    @classmethod
    def description(cls) -> str:
        """A brief description of the model."""
        return cls.model_name_abs()

    @property
    def api_key_env_var(self) -> str:
        """The environment variable for the API key."""
        return f"{self.api_key_prefix()}_API_KEY"

    def ensure_auth(self, api_key: str):
        """Sets the API key in the environment."""
        os.environ[self.api_key_env_var] = api_key.strip()


class ModelRegistry:
    """A singleton registry for AI models."""

    _instance: Optional["ModelRegistry"] = None
    _models: Dict[str, Type["BaseAiModel"]]

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._models = {}
            cls._instance._load_builtin_models()
            cls._instance._load_plugin_models()
        return cls._instance

    def register(self, model_class: Type["BaseAiModel"]):
        """Registers an AI model."""
        # Not raising error on overwrite allows for easy extension/replacement
        self._models[model_class.model_name_abs()] = model_class

    def get_model_class(self, model_name_abs: str) -> Type["BaseAiModel"]:
        """Retrieves a model class from the registry."""
        if model_name_abs not in self._models:
            raise ValueError(f"Model '{model_name_abs}' not found.")
        return self._models[model_name_abs]

    @property
    def choices(self) -> List[Tuple[str, str]]:
        """Returns a list of (display text, value) tuples for use in prompts."""
        choices_list = []
        for name, model_class in sorted(self._models.items()):
            display_text = f"{model_class.description()}"
            choices_list.append((display_text, name))
        return choices_list

    @property
    def model_names(self) -> List[str]:
        """Returns a list of available model names."""
        return list(self._models.keys())

    def _load_builtin_models(self):
        """Load built-in models"""
        for model_cls in [
            OpenAIGPT55,
            OpenAIGPT55Pro,
            AnthropicClaudeSonnet46,
            AnthropicClaudeOpus48,
            GoogleGlaGemini3Pro,
        ]:
            self.register(model_cls)

    def _load_plugin_models(self):
        """Loads models from 'opsmith.models' entry points."""
        discovered_entry_points = entry_points(group="opsmith.models")

        for entry_point in discovered_entry_points:
            try:
                model_class = entry_point.load()
                self.register(model_class)
                print(f"Loaded AI model: {model_class.name()}")
            except Exception as e:
                print(
                    "[yellow]Warning: Failed to load AI model from entry point"
                    f" '{entry_point.name}': {e}[/yellow]"
                )


class OpenAIGPT55(BaseAiModel):
    @classmethod
    def name(cls) -> str:
        return "gpt-5.5"

    @classmethod
    def provider(cls) -> str:
        return "openai"

    @classmethod
    def api_key_prefix(cls) -> str:
        return "OPENAI"

    @classmethod
    def get_model_settings(cls) -> ModelSettings:
        """Returns model-specific settings."""
        return OpenAIChatModelSettings(openai_reasoning_effort="high")


class OpenAIGPT55Pro(BaseAiModel):
    @classmethod
    def name(cls) -> str:
        return "gpt-5.5-pro"

    @classmethod
    def provider(cls) -> str:
        # Pro reasoning models are only served via the OpenAI Responses API.
        return "openai-responses"

    @classmethod
    def api_key_prefix(cls) -> str:
        return "OPENAI"

    @classmethod
    def get_model_settings(cls) -> ModelSettings:
        """Returns model-specific settings."""
        return OpenAIResponsesModelSettings(openai_reasoning_effort="medium")


class AnthropicClaudeSonnet46(BaseAiModel):
    @classmethod
    def name(cls) -> str:
        return "claude-sonnet-4-6"

    @classmethod
    def provider(cls) -> str:
        return "anthropic"

    @classmethod
    def api_key_prefix(cls) -> str:
        return "ANTHROPIC"

    @classmethod
    def get_model_settings(cls) -> ModelSettings:
        """Returns model-specific settings."""
        return AnthropicModelSettings(anthropic_effort="high")


class AnthropicClaudeOpus48(BaseAiModel):
    @classmethod
    def name(cls) -> str:
        return "claude-opus-4-8"

    @classmethod
    def provider(cls) -> str:
        return "anthropic"

    @classmethod
    def api_key_prefix(cls) -> str:
        return "ANTHROPIC"

    @classmethod
    def get_model_settings(cls) -> ModelSettings:
        """Returns model-specific settings."""
        return AnthropicModelSettings(anthropic_effort="medium")


class GoogleGlaGemini3Pro(BaseAiModel):
    @classmethod
    def name(cls) -> str:
        return "gemini-3.1-pro-preview"

    @classmethod
    def provider(cls) -> str:
        return "google-gla"

    @classmethod
    def api_key_prefix(cls) -> str:
        return "GEMINI"

    @classmethod
    def get_model_settings(cls) -> ModelSettings:
        """Returns model-specific settings."""
        return GoogleModelSettings(
            google_thinking_config=ThinkingConfigDict(thinking_level=ThinkingLevel.HIGH)
        )


MODEL_REGISTRY = ModelRegistry()
