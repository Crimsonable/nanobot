from __future__ import annotations

from pathlib import Path
from typing import Literal, TypeVar
from urllib.parse import urlparse

import yaml
from pydantic import Field, field_validator, model_validator

from agent_backend.models import StrictModel


class Profile(StrictModel):
    model: str = Field(pattern=r"^[^/\s]+/\S+$")
    instructions: str = ""
    public_roots: list[Path] = Field(default_factory=list)


class Limits(StrictModel):
    active: int = Field(default=4, ge=1, le=64)
    queued: int = Field(default=64, ge=1)
    request_seconds: int = Field(default=600, ge=1)
    file_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    spool_bytes: int = Field(default=1024 * 1024 * 1024, ge=1)
    output_bytes: int = Field(default=1024 * 1024, ge=1024)
    outbox_messages: int = Field(default=10000, ge=100)


class OpenCodeConfig(StrictModel):
    binary: Path
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    version: Literal["1.18.31"] = "1.18.31"
    port: int = Field(default=4096, ge=1024, le=65535)
    context_root: Path
    config_file: Path
    plugin_file: Path
    dependency_dir: Path


class SandboxConfig(StrictModel):
    binary: Path = Path("/usr/bin/bwrap")
    readonly_roots: list[Path] = Field(default_factory=lambda: [
        Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64"),
        Path("/etc/ld.so.cache"), Path("/etc/fonts"),
        Path("/opt/toolchain"),
    ])
    shell: str = "/bin/sh"


class RuntimeConfig(StrictModel):
    bucket_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    control_url: str
    credential_file: Path
    state_root: Path
    workspace_root: Path
    rpc_port: int = Field(default=4097, ge=1024, le=65535)
    health_port: int = Field(default=8090, ge=1024, le=65535)
    opencode: OpenCodeConfig
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    limits: Limits = Field(default_factory=Limits)
    profiles: dict[str, Profile] = Field(min_length=1)

    @field_validator("control_url")
    @classmethod
    def websocket_url(cls, value: str) -> str:
        if urlparse(value).scheme not in {"ws", "wss"}:
            raise ValueError("control_url must be ws(s)")
        return value

    @model_validator(mode="after")
    def independent_roots(self) -> RuntimeConfig:
        workspace = self.workspace_root.resolve()
        protected = [self.state_root, self.opencode.context_root, self.opencode.config_file,
                     self.opencode.plugin_file, self.opencode.dependency_dir, self.credential_file]
        if any(p.resolve().is_relative_to(workspace) for p in protected):
            raise ValueError("service files must not be inside workspace_root")
        if len({self.rpc_port, self.health_port, self.opencode.port}) != 3:
            raise ValueError("OpenCode, RPC and health ports must be distinct")
        for profile in self.profiles.values():
            for root in profile.public_roots + self.sandbox.readonly_roots:
                root = root.resolve()
                if any(root.is_relative_to(Path(p).resolve()) for p in ["/proc", "/sys", "/dev"]):
                    raise ValueError("host process, device and kernel filesystems cannot be mounted")
                if root == Path(root.anchor) or workspace.is_relative_to(root) or root.is_relative_to(workspace):
                    raise ValueError("read-only mounts must not expose workspace_root")
                if any(p.resolve().is_relative_to(root) for p in protected):
                    raise ValueError("read-only mounts must not expose service files")
        return self


class StorageConfig(StrictModel):
    endpoint: str
    bucket: str
    access_key_file: Path
    secret_key_file: Path
    region: str = "us-east-1"
    public_base_url: str = ""
    prefix: str = "attachments"
    expiry_seconds: int = Field(default=86400, ge=60)


class Frontend(StrictModel):
    provider: Literal["web", "qxt", "feishu"]
    profile_id: str
    credential_file: Path  # JSON channel-specific configuration; secrets are not env vars
    api_key_file: Path
    attachment_url_prefix: str = ""


class ControlConfig(StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    state_root: Path
    bucket_credentials: dict[str, Path]
    frontends: dict[str, Frontend]
    storage: StorageConfig
    limits: Limits = Field(default_factory=Limits)
    download_hosts: list[str] = Field(default_factory=list)
    download_private_cidrs: list[str] = Field(default_factory=list)


T = TypeVar("T", bound=StrictModel)


def load_config(path: Path, schema: type[T]) -> T:
    return schema.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 16:
        raise ValueError(f"credential is missing or too short: {path}")
    return value
