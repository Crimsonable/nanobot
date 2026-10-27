import base64
import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from mcp.server.fastmcp.exceptions import ToolError

from hazard_expert_mcp.service import ExpertService, make_s3_client


@pytest.mark.asyncio
async def test_preserves_conversation_identity_query_and_signed_urls(settings, completed_reply):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completed_reply(json.loads(request.content)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = ExpertService(settings, http, None, None)
        url = "https://files.example.com/a%20b.jpg?X-Amz-Signature=a%2Fb&x=2"
        for user, chat, query in [
            ("user-1", "chat-1", "分析图片"),
            ("user-1", "chat-1", "如何整改？"),
            ("user-1", "chat-2", "新会话"),
            ("user-2", "chat-1", "另一个用户"),
        ]:
            result = await service.analyze(user, chat, query, [url])
            assert (result.user_id, result.chat_id) == (user, chat)
            assert "bucket_id" not in result.model_dump()
            request = requests[-1]
            assert str(request.url) == "http://channel-gateway:8000/inbound-sync/web-wd"
            assert "Authorization" not in request.headers
            assert json.loads(request.content) == {
                "user_id": user,
                "chat_id": chat,
                "content": query,
                "attachments": [url],
            }
        assert len(requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 503, 504])
async def test_upstream_error_does_not_retry_or_leak_body(settings, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="SECRET INTERNAL DETAIL")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = ExpertService(settings, http, None, None)
        with pytest.raises(ToolError) as exc:
            await service.analyze("user-1", "chat-1", "分析", [])
    assert len(calls) == 1
    assert "SECRET" not in str(exc.value)


@pytest.mark.asyncio
async def test_timeout_warns_execution_may_continue(settings):
    def handler(request):
        raise httpx.ReadTimeout("test", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ToolError, match="仍在执行"):
            await ExpertService(settings, http, None, None).analyze("u", "c", "分析", [])


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, [], {}, {"status": "completed", "user_id": "other"}])
async def test_rejects_malformed_or_mismatched_replies(settings, bad):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=bad))
    ) as http:
        with pytest.raises(ToolError):
            await ExpertService(settings, http, None, None).analyze("u", "c", "分析", [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url", ["/tmp/x.jpg", "file:///tmp/x", "https://u:p@host/a", "abc", "https://[bad"]
)
async def test_invalid_attachments_never_reach_upstream(settings, url):
    service = ExpertService(settings, None, None, None)
    with pytest.raises(ToolError, match="http"):
        await service.analyze("u", "c", "分析", [url])


@pytest.mark.asyncio
async def test_upload_and_public_endpoint_signature(settings):
    storage = make_s3_client(settings, settings.minio_endpoint)
    signer = make_s3_client(settings, settings.minio_download_endpoint)
    try:
        with Stubber(storage) as stub:
            expected = {
                "Bucket": "attachments",
                "Key": ANY,
                "Body": b"hello",
                "ContentType": "text/plain",
            }
            stub.add_response("put_object", {"ETag": '"abc"'}, expected)
            stub.add_response("put_object", {"ETag": '"def"'}, expected)
            service = ExpertService(settings, None, storage, signer)
            first = await service.upload(
                "u/a", "现场.txt", base64.b64encode(b"hello").decode(), None
            )
            second = await service.upload(
                "u:a", "现场.txt", base64.b64encode(b"hello").decode(), None
            )
            stub.assert_no_pending_responses()
        assert first.size_bytes == 5
        assert first.object_key.split("/")[1] != second.object_key.split("/")[1]
        parsed = urlsplit(first.url)
        assert parsed.netloc == "files.example.com"
        assert parsed.path.startswith("/attachments/hazard-expert/")
        query = parse_qs(parsed.query)
        assert query["X-Amz-Expires"] == ["86400"]
        assert query["X-Amz-Signature"]
        assert "test-secret" not in first.url
    finally:
        storage.close()
        signer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,name", [("***", "a.jpg"), ("", "a.jpg"), ("aGk=", "../a.jpg"), ("aGk=", "a\\b.jpg")]
)
async def test_invalid_upload_is_rejected_before_s3(settings, content, name):
    with pytest.raises(ToolError):
        await ExpertService(settings, None, None, None).upload("u", name, content, None)


@pytest.mark.asyncio
async def test_decoded_size_limit(settings):
    settings.max_upload_bytes = 1
    # Both 1 and 2 bytes encode to 4 characters; enforce the decoded limit as well.
    with pytest.raises(ToolError, match="MAX_UPLOAD_BYTES"):
        await ExpertService(settings, None, None, None).upload("u", "a.txt", "aGk=", None)


@pytest.mark.asyncio
async def test_storage_failure_is_safe(settings):
    storage = Mock()
    storage.put_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "secret-detail"}}, "PutObject"
    )
    with pytest.raises(ToolError, match="MinIO 上传失败") as exc:
        await ExpertService(settings, None, storage, None).upload("u", "a.txt", "aGk=", None)
    assert "secret-detail" not in str(exc.value)
