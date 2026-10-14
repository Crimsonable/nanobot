import pytest
from mcp.server.fastmcp.exceptions import ToolError

from engineering_scene_mcp.service import (
    build_chat_completions_url,
    build_vllm_payload,
    classify_media_type,
)


def test_build_chat_completions_url_appends_endpoint() -> None:
    assert build_chat_completions_url("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000/v1/chat/completions"


def test_build_chat_completions_url_handles_trailing_slash() -> None:
    assert build_chat_completions_url("http://127.0.0.1:8000/v1/") == "http://127.0.0.1:8000/v1/chat/completions"


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("image/jpeg", "image"),
        ("video/mp4; charset=binary", "video"),
    ],
)
def test_classify_media_type(
    content_type: str | None,
    expected: str,
) -> None:
    assert classify_media_type(content_type) == expected


def test_classify_media_type_rejects_non_media() -> None:
    with pytest.raises(ToolError, match="Content-Type"):
        classify_media_type("application/pdf")


def test_build_vllm_payload_uses_image_url_block() -> None:
    payload = build_vllm_payload(
        prompt="inspect this",
        media_url="https://files.example/photo.jpg",
        media_type="image",
    )

    media_block = payload["messages"][1]["content"][1]
    assert media_block == {
        "type": "image_url",
        "image_url": {"url": "https://files.example/photo.jpg"},
    }


def test_build_vllm_payload_uses_video_url_block() -> None:
    payload = build_vllm_payload(
        prompt="inspect this",
        media_url="https://files.example/clip.mp4",
        media_type="video",
    )

    media_block = payload["messages"][1]["content"][1]
    assert media_block == {
        "type": "video_url",
        "video_url": {"url": "https://files.example/clip.mp4"},
    }


@pytest.mark.parametrize("content_type", [None, "application/octet-stream"])
def test_media_type_is_not_guessed(content_type):
    with pytest.raises(ToolError, match="Content-Type"):
        classify_media_type(content_type)
