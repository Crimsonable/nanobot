from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal, TypeVar
from urllib.parse import urlparse

import yaml
from pydantic import Field, SecretStr, field_validator, model_validator

from agent_backend.common import frontend_common_root, frontend_segment, scoped_public_path
from agent_backend.models import StrictModel, digest
from agent_backend.workspaces import workspace_volume_subpath

BUCKET_SUFFIX_LENGTH = 12


def bucket_name_prefix(name_prefix: str, frontend_id: str) -> str:
    frontend = re.sub(r"[^a-z0-9-]+", "-", frontend_id.lower()).strip("-") or digest(frontend_id)[:8]
    available = 63 - len(name_prefix) - BUCKET_SUFFIX_LENGTH - 2
    return f"{name_prefix}-{frontend[:available].rstrip('-')}"


def valid_bucket_id(name_prefix: str, frontend_id: str, bucket_id: str) -> bool:
    prefix, _, suffix = bucket_id.rpartition("-")
    return (prefix in (name_prefix, bucket_name_prefix(name_prefix, frontend_id))
            and len(suffix) == BUCKET_SUFFIX_LENGTH and re.fullmatch(r"[0-9a-f]+", suffix) is not None)


class AttachmentLimits(StrictModel):
    file_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    spool_bytes: int = Field(default=1024 * 1024 * 1024, ge=1)


class Limits(AttachmentLimits):
    request_seconds: int = Field(default=600, ge=1)


class OpenCodeConfig(StrictModel):
    binary: Path = Path("/opt/opencode/opencode")
    port: int = Field(default=4096, ge=1024, le=65535)
    context_root: Path = Path("/opt/runtime-context")
    config_file: Path = Path("/mnt/common/{frontend_id}/config/opencode-server.json")
    plugin_dir: Path = Path("/mnt/source/opencode_plugin")


class SessionLoggingConfig(StrictModel):
    enabled: bool = True
    text: bool = True
    reasoning: bool = True
    tools: bool = True
    tool_arguments: bool = True
    tool_output: bool = True
    max_chars: int = Field(default=16000, ge=256, le=200000)


class SandboxConfig(StrictModel):
    binary: Path = Path("/usr/bin/bwrap")
    readonly_roots: list[Path] = Field(default_factory=lambda: [
        Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64"),
        Path("/etc/ld.so.cache"), Path("/etc/fonts"),
        Path("/opt/toolchain"),
    ])
    shell: str = "/bin/sh"


class StorageConfig(StrictModel):
    provider: Literal["minio"] = "minio"
    endpoint: str
    bucket: str
    access_key_file: Path | None = None
    secret_key_file: Path | None = None
    access_key: SecretStr | None = None
    secret_key: SecretStr | None = None
    region: str = "us-east-1"
    public_base_url: str = ""
    key_prefix: str = "attachments"
    presign_expiry_seconds: int = Field(default=86400, ge=60)

    @model_validator(mode="after")
    def credentials(self) -> StorageConfig:
        for name in ("access_key", "secret_key"):
            if (getattr(self, name) is None) == (getattr(self, name + "_file") is None):
                raise ValueError(f"provide exactly one of {name} or {name}_file")
        return self

    def credential(self, name: str) -> str:
        value = getattr(self, name)
        return value.get_secret_value() if value is not None else getattr(self, name + "_file").read_text().strip()


class GatewaySettings(StrictModel):
    """Internal service defaults, independent of the frontend registry."""

    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    state_root: Path = Path("/data/gateway")
    control_url: str = "http://127.0.0.1:8001"

    @model_validator(mode="after")
    def transport_urls(self):
        if urlparse(self.control_url).scheme not in {"http", "https"}:
            raise ValueError("gateway control_url must be http(s)")
        return self


class BucketSettings(StrictModel):
    gateway_url: str = "http://channel-gateway:8000"
    state_root: Path = Path("/data/runtime")
    workspace_root: Path = Path("/mnt/env/workspaces")
    common_root: Path = Path("/mnt/common")
    opencode: OpenCodeConfig = Field(default_factory=OpenCodeConfig)
    permissions_file: Path | None = None
    logging_file: Path | None = None
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    limits: AttachmentLimits = Field(default_factory=AttachmentLimits)

    @field_validator("gateway_url")
    @classmethod
    def gateway_http(cls, value: str) -> str:
        if urlparse(value).scheme not in {"http", "https"}:
            raise ValueError("gateway_url must be http(s)")
        return value


class BucketConfig(BucketSettings):
    bucket_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    frontend_id: str = Field(min_length=1)
    frontend_config_file: Path | None = None

    @model_validator(mode="after")
    def independent_roots(self) -> BucketConfig:
        if self.frontend_id:
            frontend_segment(self.frontend_id)
            for obj, name in [(self.opencode, "config_file"), (self, "permissions_file"),
                              (self, "logging_file")]:
                value = getattr(obj, name)
                if value is not None:
                    setattr(obj, name, Path(value.as_posix().replace("{frontend_id}", self.frontend_id)))
        workspace = self.workspace_root.resolve()
        protected = [self.state_root, self.opencode.context_root, self.opencode.config_file,
                     self.opencode.plugin_dir]
        if self.permissions_file:
            protected.append(self.permissions_file)
        if self.logging_file:
            protected.append(self.logging_file)
        if self.frontend_config_file and self.frontend_config_file.resolve().is_relative_to(workspace):
            raise ValueError("frontend config must not be inside workspace_root")
        if any(p.resolve().is_relative_to(workspace) for p in protected):
            raise ValueError("service files must not be inside workspace_root")
        for root in self.sandbox.readonly_roots:
            root = scoped_public_path(root, self.common_root, self.frontend_id)
            if any(root.is_relative_to(Path(p).resolve()) for p in ["/proc", "/sys", "/dev"]):
                raise ValueError("host process, device and kernel filesystems cannot be mounted")
            if root == Path(root.anchor) or workspace.is_relative_to(root) or root.is_relative_to(workspace):
                raise ValueError("read-only mounts must not expose workspace_root")
            if any(p.resolve().is_relative_to(root) for p in protected):
                raise ValueError("read-only mounts must not expose service files")
        return self


class Frontend(StrictModel):
    provider: Literal["web", "qxt", "feishu"]
    channel_config: dict = Field(default_factory=dict, repr=False)
    attachment_url_prefix: str = ""
    attachment_storage: StorageConfig | None = Field(default=None, repr=False)


class BucketPoolConfig(StrictModel):
    namespace: str = "opencode-sv"
    name_prefix: str = Field(default="opencode-bucket", pattern=r"^[a-z][a-z0-9-]{0,35}$")
    image: str = "agent-opencode:opencode-2.3.2-bwrap011"
    image_pull_policy: Literal["Always", "IfNotPresent", "Never"] = "IfNotPresent"
    max_sessions_per_bucket: int = Field(default=16, ge=1)
    ready_timeout_seconds: int = Field(default=120, ge=1)
    sweep_interval_seconds: int = Field(default=10, ge=1)
    opencode_host_path: str = "/data/opencode_backend_k8s/opencode"
    data_pvc: str = "opencode-data-pvc"
    source_pvc: str = "opencode-source-pvc"
    common_pvc: str = "opencode-common-pvc"
    state_pvc: str = "opencode-state-pvc"
    node_selector: dict[str, str] = Field(default_factory=lambda: {"opencode-runtime": "true"})
    api_url: str = "https://kubernetes.default.svc"
    token_file: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_file: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")


class ControlConfig(StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8001, ge=1, le=65535)
    state_root: Path = Path("/data/control")
    frontends: dict[str, Frontend]
    limits: Limits = Field(default_factory=Limits)
    bucket_pool: BucketPoolConfig = Field(default_factory=BucketPoolConfig)
    bucket_templates: dict[str, BucketSettings] = Field(default_factory=dict)
    frontend_idle_ttl_seconds: dict[str, int] = Field(default_factory=dict)
    frontend_config_path: Path = Path("/mnt/common/frontends.json")

    def bucket_for(self, frontend_id: str) -> BucketSettings:
        if frontend_id not in self.frontends:
            raise ValueError("frontend is not registered")
        if frontend_id not in self.bucket_templates:
            raise ValueError("frontend runtime is not configured")
        runtime = self.bucket_templates[frontend_id]
        return runtime

    def deployment_for(self, frontend_id: str, bucket_id: str) -> BucketConfig:
        selected = FrontendConfig(frontend_id=frontend_id,
            provider=self.frontends[frontend_id].provider,
            bucket=self.bucket_for(frontend_id).model_dump(),
            control=ControlSettings(bucket_pool=self.bucket_pool))
        return selected.bucket_config(self.frontend_config_path, bucket_id)


class ControlSettings(StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8001, ge=1, le=65535)
    state_root: Path = Path("/data/control")
    bucket_pool: BucketPoolConfig = Field(default_factory=BucketPoolConfig)
    limits: Limits = Field(default_factory=Limits)


class DeploymentBucketSettings(BucketSettings):
    permissions_file: Path | None = Path("/mnt/common/{frontend_id}/config/user-permissions.yaml")


class FrontendSettings(StrictModel):
    """Routing and runtime settings belonging to one frontend registry entry."""

    frontend_id: str = Field(min_length=1)
    provider: Literal["web", "qxt", "feishu"]
    send_msg_url: str = ""
    attachment_storage: StorageConfig | None = None
    instance_idle_ttl_seconds: int = Field(default=3600, ge=0)
    attachment_url_prefix: str = ""
    channel: dict = Field(default_factory=dict, repr=False)
    bucket: DeploymentBucketSettings

    @model_validator(mode="after")
    def validate_frontend(self) -> FrontendSettings:
        frontend_segment(self.frontend_id)
        allowed = {"web": set(), "feishu": {"app_id", "app_secret"},
                   "qxt": {"app_id", "app_secret", "corp_id", "callback_token", "access_url"}}
        if unknown := set(self.channel) - allowed[self.provider]:
            raise ValueError("unsupported channel configuration: " + ", ".join(sorted(unknown)))
        return self


class FrontendConfig(FrontendSettings):
    """A selected frontend plus CU infrastructure settings."""

    control: ControlSettings = Field(default_factory=ControlSettings)

    @model_validator(mode="after")
    def deployment_settings(self) -> FrontendConfig:
        pool = self.control.bucket_pool
        if not pool.node_selector:
            raise ValueError("host-mounted buckets require node_selector for the storage node")
        if not self.bucket.state_root.is_relative_to(Path("/data")) or self.bucket.state_root == Path("/data"):
            raise ValueError("runtime state_root must be below /data")
        workspace_volume_subpath(self.bucket.workspace_root, self.frontend_id)
        return self

    def control_config(self) -> ControlConfig:
        channel = {**self.channel, "send_msg_url": self.send_msg_url}
        frontend = Frontend(provider=self.provider,
                            channel_config=channel,
                            attachment_storage=self.gateway_storage(),
                            attachment_url_prefix=self.attachment_url_prefix)
        return ControlConfig(**self.control.model_dump(), frontends={self.frontend_id: frontend},
                             bucket_templates={self.frontend_id: self.bucket},
                             frontend_idle_ttl_seconds={self.frontend_id: self.instance_idle_ttl_seconds})

    def gateway_storage(self) -> StorageConfig | None:
        storage = self.attachment_storage
        if storage is None:
            return None
        root = frontend_common_root(self.bucket.common_root, self.frontend_id) / "config"
        values = {}
        for name in ("access_key_file", "secret_key_file"):
            value = getattr(storage, name)
            if value is None:
                continue
            text = value.as_posix().replace("{frontend_id}", self.frontend_id)
            if text.startswith("/mnt/common/"):
                text = self.bucket.common_root.as_posix() + text[len("/mnt/common"):]
            target = Path(os.path.abspath(text))
            if not target.is_relative_to(root) or target == root or target.resolve() != target:
                raise ValueError("storage credentials must belong to this frontend config directory without symbolic links")
            values[name] = target
        return storage.model_copy(update=values)

    def bucket_config(self, path: Path, bucket_id: str) -> BucketConfig:
        values = self.bucket.model_dump()
        own_root = frontend_common_root(self.bucket.common_root, self.frontend_id)

        def frontend_path(value: Path | None, directory: str) -> Path | None:
            if value is None:
                return None
            text = value.as_posix().replace("{frontend_id}", self.frontend_id)
            if value.as_posix().startswith("/mnt/common/{frontend_id}/") or value.as_posix() == "/mnt/common/{frontend_id}":
                text = self.bucket.common_root.as_posix() + text[len("/mnt/common"):]
            target = Path(text)
            # Both the lexical path and its resolution must stay in the correct
            # subtree. In particular, a config/ symlink must not expose app/ files.
            absolute = Path(os.path.abspath(target))
            root = own_root / directory
            if not absolute.is_relative_to(root) or absolute.resolve() != absolute:
                raise ValueError(f"path must belong to this frontend {directory} directory without symbolic links")
            if directory == "config" and absolute == root:
                raise ValueError("configuration must name a file below the frontend config directory")
            return target

        values["opencode"]["config_file"] = frontend_path(self.bucket.opencode.config_file, "config")
        values["permissions_file"] = frontend_path(self.bucket.permissions_file, "config")
        values["logging_file"] = frontend_path(self.bucket.logging_file, "config")
        self.gateway_storage()  # Validate deployment paths; upload credentials stay in Gateway.
        pool = self.control.bucket_pool
        if not valid_bucket_id(pool.name_prefix, self.frontend_id, bucket_id):
            raise ValueError("runtime requires a scheduler-issued --bucket-id")
        values["state_root"] = self.bucket.state_root / self.frontend_id / bucket_id
        return BucketConfig(**values, bucket_id=bucket_id, frontend_id=self.frontend_id,
                             frontend_config_file=path.resolve())


class FrontendsConfig(StrictModel):
    """Shared CU registry; runtime/model/permissions are selected by frontend ID."""

    control: ControlSettings = Field(default_factory=ControlSettings)
    frontends: list[FrontendSettings] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_frontends(self) -> FrontendsConfig:
        ids = [frontend.frontend_id for frontend in self.frontends]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate frontend id")
        return self

    def frontend(self, frontend_id: str) -> FrontendConfig:
        entry = next((frontend for frontend in self.frontends if frontend.frontend_id == frontend_id), None)
        if entry is None:
            raise ValueError("--frontend-id must identify a registered frontend")
        return FrontendConfig.model_validate({**entry.model_dump(), "control": self.control.model_dump()})

    def control_config(self) -> ControlConfig:
        selected = [self.frontend(f.frontend_id) for f in self.frontends]
        config = selected[0].control_config()
        config.frontends = {f.frontend_id: f.control_config().frontends[f.frontend_id] for f in selected}
        config.bucket_templates = {f.frontend_id: f.bucket for f in selected}
        config.frontend_idle_ttl_seconds = {f.frontend_id: f.instance_idle_ttl_seconds for f in selected}
        return config

    def bucket_config(self, path: Path, bucket_id: str,
                       frontend_id: str) -> BucketConfig:
        return self.frontend(frontend_id).bucket_config(path, bucket_id)


def load_frontends(path: Path) -> FrontendsConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return FrontendsConfig.model_validate(raw)


T = TypeVar("T", bound=StrictModel)


def load_config(path: Path, schema: type[T]) -> T:
    return schema.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
