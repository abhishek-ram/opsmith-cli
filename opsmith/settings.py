"""Process-wide settings, read from ``.opsmith.conf.yml``.

The file is resolved against the working directory and read once, at import time, which is why
``--src-dir`` does not move it: a run that wants the file read has to start in the project root.
"""

from typing import Optional

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class OpsmithSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPSMITH_", yaml_file=".opsmith.conf.yml")

    deployments_dir: str = ".opsmith"
    config_filename: str = "deployments.yml"
    max_dockerfile_gen_attempts: int = 3
    max_docker_compose_gen_attempts: int = 3

    #: The last place :mod:`opsmith.core.llm` looks for the model, after the option and the
    #: environment.
    model: Optional[str] = None

    #: Declared so that a key written into the file is refused with an explanation rather than
    #: crashing the import on pydantic-settings' ``extra="forbid"``. It is never used as a key.
    api_key: Optional[str] = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (YamlConfigSettingsSource(settings_cls),)


settings = OpsmithSettings()
