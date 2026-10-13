from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, TypeVar
from urllib.parse import urlparse

import yaml
from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator

from agent_backend.common import frontend_common_root, frontend_segment, scoped_public_path
from agent_backend.models import StrictModel
from agent_backend.workspaces import workspace_volume_subpath


class UserSandbox(StrictModel):
    enabled: bool = True
    network: bool = False


class UserSession(StrictModel):
    enabled: bool = True
    default_tools: bool = True
    tools: dict[str, bool] = Field(default_factory=dict)


class UserFilesystem(StrictModel):
    workspace: Literal["read_write", "read_only", "none"] = "read_write"
    # None inherits the profile's public roots; [] explicitly grants none.
    public_roots: list[Path] | None = None


class UserPolicy(StrictModel):
    sandbox: UserSandbox = Field(default_factory=UserSandbox)
    session: UserSession = Field(default_factory=UserSession)
    filesystem: UserFilesystem = Field(default_factory=UserFilesystem)


class UserPermissions(UserPolicy):
    frontend_id: str | None = None
    user_id: str


class PermissionConfig(StrictModel):
    defaults: UserPolicy = Field(default_factory=UserPolicy)
    users: list[UserPermissions] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_users(self) -> PermissionConfig:
        keys = [(user.frontend_id, user.user_id) for user in self.users]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate user permission configuration")
        return self

    def for_user(self, frontend_id: str, user_id: str) -> UserPolicy:
        values = self.defaults.model_dump()
        for user in self.users:
            if user.frontend_id in {None, frontend_id} and user.user_id == user_id:
                patch = user.model_dump(exclude_unset=True, exclude={"frontend_id", "user_id"})
                for section, overrides in patch.items():
                    if section == "session" and "tools" in overrides:
                        overrides["tools"] = {**values[section]["tools"], **overrides["tools"]}
                    values[section].update(overrides)
        return UserPolicy.model_validate(values)


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
    binary: Path = Path("/opt/opencode/opencode")
    port: int = Field(default=4096, ge=1024, le=65535)
    context_root: Path = Path("/opt/runtime-context")
    config_file: Path = Path("/mnt/common/{frontend_id}/config/opencode-server.json")
    plugin_file: Path = Path("/mnt/source/opencode_plugin/dist/index.mjs")
    dependency_dir: Path = Path("/mnt/source/opencode_plugin")


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
    prefix: str = Field(default="attachments", validation_alias=AliasChoices("key_prefix", "prefix"))
    expiry_seconds: int = Field(default=86400, ge=60,
                               validation_alias=AliasChoices("presign_expiry_seconds", "expiry_seconds"))

    @model_validator(mode="after")
    def credentials(self) -> StorageConfig:
        for name in ("access_key", "secret_key"):
            if (getattr(self, name) is None) == (getattr(self, name + "_file") is None):
                raise ValueError(f"provide exactly one of {name} or {name}_file")
        return self

    def credential(self, name: str) -> str:
        value = getattr(self, name)
        return value.get_secret_value() if value is not None else getattr(self, name + "_file").read_text().strip()


class RuntimeSettings(StrictModel):
    control_url: str = "ws://container-up:8000/internal/buckets/ws"
    state_root: Path = Path("/data/runtime")
    workspace_root: Path = Path("/mnt/env/workspaces")
    workspace_layout: Literal["identity_hash", "frontend_user"] = "identity_hash"
    workspace_template_root: Path | None = None
    common_root: Path = Path("/mnt/common")
    rpc_port: int = Field(default=4097, ge=1024, le=65535)
    health_port: int = Field(default=8090, ge=1024, le=65535)
    opencode: OpenCodeConfig = Field(default_factory=OpenCodeConfig)
    permissions_file: Path | None = None
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    limits: Limits = Field(default_factory=Limits)
    profiles: dict[str, Profile] = Field(min_length=1)

    @field_validator("control_url")
    @classmethod
    def websocket_url(cls, value: str) -> str:
        if urlparse(value).scheme not in {"ws", "wss"}:
            raise ValueError("control_url must be ws(s)")
        return value


class RuntimeConfig(RuntimeSettings):
    bucket_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    frontend_id: str | None = None
    storage: StorageConfig | None = None
    frontend_config_file: Path | None = None

    @model_validator(mode="after")
    def independent_roots(self) -> RuntimeConfig:
        if self.frontend_id:
            frontend_segment(self.frontend_id)
            for obj, name in [(self.opencode, "config_file"), (self, "permissions_file"),
                              (self, "workspace_template_root")]:
                value = getattr(obj, name)
                if value is not None:
                    setattr(obj, name, Path(value.as_posix().replace("{frontend_id}", self.frontend_id)))
        workspace = self.workspace_root.resolve()
        protected = [self.state_root, self.opencode.context_root, self.opencode.config_file,
                     self.opencode.plugin_file, self.opencode.dependency_dir]
        if self.permissions_file:
            protected.append(self.permissions_file)
        if self.frontend_config_file and self.frontend_config_file.resolve().is_relative_to(workspace):
            raise ValueError("frontend config must not be inside workspace_root")
        if self.storage:
            protected.extend(p for p in [self.storage.access_key_file, self.storage.secret_key_file] if p)
        if any(p.resolve().is_relative_to(workspace) for p in protected):
            raise ValueError("service files must not be inside workspace_root")
        if len({self.rpc_port, self.health_port, self.opencode.port}) != 3:
            raise ValueError("OpenCode, RPC and health ports must be distinct")
        for profile in self.profiles.values():
            for root in profile.public_roots + self.sandbox.readonly_roots:
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
    profile_id: str
    credential_file: Path | None = None
    channel_config: dict = Field(default_factory=dict, repr=False)
    attachment_url_prefix: str = ""


class BucketPoolConfig(StrictModel):
    namespace: str = "opencode-sv"
    name_prefix: str = Field(default="opencode-bucket", pattern=r"^[a-z][a-z0-9-]{0,35}$")
    image: str = "agent-bucket-runtime:opencode-1.18.31"
    image_pull_policy: Literal["Always", "IfNotPresent", "Never"] = "IfNotPresent"
    max_users_per_bucket: int = Field(default=16, ge=1)
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
    port: int = Field(default=8000, ge=1, le=65535)
    state_root: Path = Path("/data/control")
    bucket_ids: list[str] = Field(default_factory=list)
    frontends: dict[str, Frontend]
    limits: Limits = Field(default_factory=Limits)
    bucket_pool: BucketPoolConfig | None = None
    runtime_template: RuntimeSettings | None = None
    runtime_templates: dict[str, RuntimeSettings] = Field(default_factory=dict)
    frontend_idle_ttl_seconds: dict[str, int] = Field(default_factory=dict)
    frontend_config_path: Path = Path("/mnt/common/frontends.json")
    instance_idle_ttl_seconds: int = 3600

    def runtime_for(self, frontend_id: str) -> RuntimeSettings:
        if frontend_id not in self.frontends:
            raise ValueError("frontend is not registered")
        runtime = self.runtime_templates.get(frontend_id) if self.runtime_templates else self.runtime_template
        if runtime is None:
            raise ValueError("frontend runtime is not configured")
        return runtime


class ControlSettings(StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    state_root: Path = Path("/data/control")
    bucket_ids: list[str] = Field(default_factory=list)
    bucket_pool: BucketPoolConfig | None = Field(default_factory=BucketPoolConfig)
    limits: Limits = Field(default_factory=Limits)


class DeploymentProfile(Profile):
    public_roots: list[Path] = Field(default_factory=lambda: [Path("/mnt/common/{frontend_id}/app"), Path("/mnt/source/public")])


class DeploymentRuntimeSettings(RuntimeSettings):
    workspace_layout: Literal["identity_hash", "frontend_user"] = "frontend_user"
    workspace_template_root: Path | None = Path("/mnt/common/{frontend_id}/app/templates")
    permissions_file: Path | None = Path("/mnt/common/{frontend_id}/config/user-permissions.yaml")
    profiles: dict[str, DeploymentProfile] = Field(min_length=1)


class FrontendSettings(StrictModel):
    """Routing and runtime settings belonging to one frontend registry entry."""

    id: str = Field(min_length=1)
    provider: Literal["web", "qxt", "feishu"]
    send_msg_url: str = ""
    attachment_storage: StorageConfig | None = None
    instance_idle_ttl_seconds: int = Field(default=3600, ge=0)
    profile_id: str = "reports"
    attachment_url_prefix: str = ""
    channel: dict = Field(default_factory=dict, repr=False)
    runtime: DeploymentRuntimeSettings

    @model_validator(mode="after")
    def profile_exists(self) -> FrontendSettings:
        frontend_segment(self.id)
        if self.profile_id not in self.runtime.profiles:
            raise ValueError("profile_id must exist in runtime.profiles")
        return self


class FrontendConfig(FrontendSettings):
    """A selected frontend plus CU infrastructure settings."""

    control: ControlSettings = Field(default_factory=ControlSettings)

    @model_validator(mode="after")
    def deployment_settings(self) -> FrontendConfig:
        if self.control.bucket_pool:
            pool = self.control.bucket_pool
            if not pool.node_selector:
                raise ValueError("host-mounted buckets require node_selector for the storage node")
            if not self.runtime.state_root.is_relative_to(Path("/data")) or self.runtime.state_root == Path("/data"):
                raise ValueError("dynamic runtime state_root must be below /data")
            workspace_volume_subpath(self.runtime.workspace_root, self.id)
            if self.control.bucket_ids:
                raise ValueError("dynamic pool does not use static bucket_ids")
        return self

    def control_config(self) -> ControlConfig:
        channel = {**self.channel, "send_msg_url": self.send_msg_url}
        frontend = Frontend(provider=self.provider, profile_id=self.profile_id,
                            channel_config=channel,
                            attachment_url_prefix=self.attachment_url_prefix)
        return ControlConfig(**self.control.model_dump(), frontends={self.id: frontend},
                             runtime_template=self.runtime,
                             instance_idle_ttl_seconds=self.instance_idle_ttl_seconds)

    def runtime_config(self, path: Path, bucket_id: str | None = None) -> RuntimeConfig:
        values = self.runtime.model_dump()
        own_root = frontend_common_root(self.runtime.common_root, self.id)

        def frontend_path(value: Path | None, directory: str) -> Path | None:
            if value is None:
                return None
            text = value.as_posix().replace("{frontend_id}", self.id)
            if value.as_posix().startswith("/mnt/common/{frontend_id}/") or value.as_posix() == "/mnt/common/{frontend_id}":
                text = self.runtime.common_root.as_posix() + text[len("/mnt/common"):]
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

        values["opencode"]["config_file"] = frontend_path(self.runtime.opencode.config_file, "config")
        values["permissions_file"] = frontend_path(self.runtime.permissions_file, "config")
        values["workspace_template_root"] = frontend_path(self.runtime.workspace_template_root, "app")
        storage = self.attachment_storage
        if storage:
            storage = storage.model_copy(update={name: frontend_path(getattr(storage, name), "config")
                for name in ["access_key_file", "secret_key_file"]})
        for profile in values["profiles"].values():
            profile["public_roots"] = [
                frontend_path(root, "app") if root.as_posix().startswith("/mnt/common/{frontend_id}/")
                or root.as_posix() == "/mnt/common/{frontend_id}" else root
                for root in profile["public_roots"]]
        if self.control.bucket_pool:
            import re
            pool = self.control.bucket_pool
            if not bucket_id or not re.fullmatch(re.escape(pool.name_prefix) + r"-[0-9a-f]{12}", bucket_id):
                raise ValueError("dynamic runtime requires a scheduler-issued --bucket-id")
            values["state_root"] = self.runtime.state_root / self.id / bucket_id
            return RuntimeConfig(**values, bucket_id=bucket_id, frontend_id=self.id, storage=storage,
                                 frontend_config_file=path.resolve())
        buckets = self.control.bucket_ids
        if bucket_id is None:
            if len(buckets) != 1:
                raise ValueError("--bucket-id is required when multiple buckets are configured")
            bucket_id = next(iter(buckets))
        if bucket_id not in buckets:
            raise ValueError("bucket ID is not registered in control.bucket_ids")
        return RuntimeConfig(**values, bucket_id=bucket_id, frontend_id=self.id,
                             storage=storage,
                             frontend_config_file=path.resolve())


class FrontendsConfig(StrictModel):
    """Shared CU registry; runtime/model/permissions are selected by frontend ID."""

    control: ControlSettings = Field(default_factory=ControlSettings)
    frontends: list[FrontendSettings] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_frontends(self) -> FrontendsConfig:
        ids = [frontend.id for frontend in self.frontends]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate frontend id")
        if len(ids) > 1 and not self.control.bucket_pool:
            raise ValueError("multiple frontends require the dynamic bucket pool")
        return self

    def frontend(self, frontend_id: str | None) -> FrontendConfig:
        if frontend_id is None and len(self.frontends) == 1:
            frontend_id = self.frontends[0].id
        entry = next((frontend for frontend in self.frontends if frontend.id == frontend_id), None)
        if entry is None:
            raise ValueError("--frontend-id must identify a registered frontend")
        return FrontendConfig.model_validate({**entry.model_dump(), "control": self.control.model_dump()})

    def control_config(self) -> ControlConfig:
        selected = [self.frontend(f.id) for f in self.frontends]
        config = selected[0].control_config()
        config.frontends = {f.id: f.control_config().frontends[f.id] for f in selected}
        config.runtime_templates = {f.id: f.runtime for f in selected}
        config.frontend_idle_ttl_seconds = {f.id: f.instance_idle_ttl_seconds for f in selected}
        return config

    def runtime_config(self, path: Path, bucket_id: str | None = None,
                       frontend_id: str | None = None) -> RuntimeConfig:
        return self.frontend(frontend_id).runtime_config(path, bucket_id)


def load_frontends(path: Path) -> FrontendsConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return FrontendsConfig.model_validate(raw)


T = TypeVar("T", bound=StrictModel)


def load_config(path: Path, schema: type[T]) -> T:
    return schema.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
