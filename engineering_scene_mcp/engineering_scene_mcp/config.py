from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, BaseModel, Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class VLLMConfig(BaseModel):
    api_url: HttpUrl
    api_key: str = Field(min_length=1)
    model: str = Field(min_length=1)


class TransportSecurityConfig(BaseModel):
    enable_dns_rebinding_protection: bool = True
    allowed_hosts: list[str] = Field(
        default_factory=lambda: [
            "localhost",
            "localhost:*",
            "127.0.0.1",
            "127.0.0.1:*",
        ]
    )
    allowed_origins: list[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    service_name: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    host: str = "0.0.0.0"
    system_prompt: str = Field(min_length=1)
    vllm: VLLMConfig
    transport_security: TransportSecurityConfig = Field(default_factory=TransportSecurityConfig)
    request_timeout_seconds: float = Field(default=120.0, gt=0.0)
    default_temperature: float = 0.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ENGINEERING_SCENE_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = Field(
        default=8090,
        validation_alias=AliasChoices("ENGINEERING_SCENE_MCP_PORT"),
    )
    config_path: Path = Field(
        default=Path("config/service.json"),
        validation_alias=AliasChoices("ENGINEERING_SCENE_MCP_CONFIG_PATH"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_app_config() -> AppConfig:
    settings = get_settings()
    raw_config = json.loads(settings.config_path.read_text(encoding="utf-8"))
    return AppConfig.model_validate(raw_config)
