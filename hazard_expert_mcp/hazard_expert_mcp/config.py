from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Deployment settings come from the Compose or Kubernetes environment."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    mcp_host: str = "0.0.0.0"
    mcp_port: int = Field(default=8082, ge=1, le=65535)
    mcp_api_key: SecretStr = SecretStr("")
    log_level: str = "info"

    container_up_base_url: str
    container_up_frontend_id: str = Field(min_length=1)
    container_up_timeout_seconds: float = Field(default=720, gt=0)
    container_up_connect_timeout_seconds: float = Field(default=10, gt=0)

    minio_endpoint: str
    minio_download_endpoint: str = ""
    minio_access_key: SecretStr = Field(min_length=1)
    minio_secret_key: SecretStr = Field(min_length=1)
    minio_bucket: str = Field(min_length=1)
    minio_region: str = "us-east-1"
    minio_key_prefix: str = "hazard-expert"
    minio_presign_expiry_seconds: int = Field(default=86400, ge=1, le=604800)
    minio_connect_timeout_seconds: int = Field(default=10, gt=0)
    minio_read_timeout_seconds: int = Field(default=120, gt=0)
    max_upload_bytes: int = Field(default=26214400, gt=0)

    @field_validator("container_up_base_url", "minio_endpoint", "minio_download_endpoint")
    @classmethod
    def http_url(cls, value: str, info) -> str:
        if not value and info.field_name == "minio_download_endpoint":
            return ""
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("must be an http(s) URL without credentials, query or fragment")
        if info.field_name.startswith("minio_") and parsed.path not in {"", "/"}:
            raise ValueError("MinIO endpoints must be an S3 origin without a path prefix")
        return value.rstrip("/")

    @field_validator("container_up_frontend_id", "minio_bucket", "minio_region")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if value != value.strip() or not value:
            raise ValueError("must be nonempty without surrounding whitespace")
        return value

    @property
    def max_request_bytes(self) -> int:
        # Base64 overhead plus room for the JSON-RPC envelope and other arguments.
        return 4 * ((self.max_upload_bytes + 2) // 3) + 65536
