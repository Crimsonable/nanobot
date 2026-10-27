import pytest

from hazard_expert_mcp.config import Settings


@pytest.fixture
def settings():
    return Settings(
        container_up_base_url="http://channel-gateway:8000",
        container_up_frontend_id="web-wd",
        minio_endpoint="http://minio:9000",
        minio_download_endpoint="https://files.example.com",
        minio_access_key="test-access",
        minio_secret_key="test-secret",
        minio_bucket="attachments",
        mcp_api_key="test-service-token",
    )


@pytest.fixture
def completed_reply():
    def build(body):
        return {
            "status": "completed",
            "frontend_id": "web-wd",
            "user_id": body["user_id"],
            "chat_id": body["chat_id"],
            "content": "发现临边防护缺失。",
            "attachments": [{"url": "https://files.example.com/report.pdf"}],
            "bucket_id": "internal-bucket",
            "identity_id": "internal-instance",
        }

    return build
