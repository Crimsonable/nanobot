from __future__ import annotations

import re

from pydantic import Field, field_validator, model_validator

from agent_backend.common import frontend_segment
from agent_backend.models import StrictModel


class SkillSpec(StrictModel):
    repo_id: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    asset_id: str = Field(min_length=1)

    @property
    def target_name(self) -> str:
        return self.repo_id.split("/", 1)[1]

    @model_validator(mode="after")
    def valid_name(self) -> SkillSpec:
        frontend_segment(self.target_name)
        return self


class McpSpec(StrictModel):
    asset_id: str = Field(min_length=1)
    name: str = Field(min_length=1)

    @field_validator("asset_id", mode="before")
    @classmethod
    def string_id(cls, value: object) -> str:
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not re.fullmatch(r"[\w-][\w.-]{0,199}", value):
            raise ValueError("MCP name must be a single name")
        return value


class Assets(StrictModel):
    skills: list[SkillSpec] = Field(default_factory=list)
    mcp: list[McpSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_targets(self) -> Assets:
        for names in ([item.target_name for item in self.skills], [item.name for item in self.mcp]):
            if len(names) != len(set(names)):
                raise ValueError("duplicate asset target name")
        return self
