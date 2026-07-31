from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import unquote, urlparse

import httpx
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, HttpUrl

from engineering_scene_mcp.config import get_app_config, get_settings

MediaType = Literal["image", "video"]

IMAGE_SUFFIXES = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
}
VIDEO_SUFFIXES = {
    ".3gp",
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ts",
    ".webm",
    ".wmv",
}


class AnalyzeImageInput(BaseModel):
    prompt: str = Field(min_length=1, description="User prompt for the engineering scene analysis.")
    media_url: HttpUrl = Field(
        description="Publicly reachable image or video URL for the model to inspect."
    )


class AnalyzeImageOutput(BaseModel):
    result: str = Field(description="Model response returned from the multimodal vLLM backend.")


def build_chat_completions_url(api_url: str) -> str:
    return f"{api_url.rstrip('/')}/chat/completions"


def classify_media_type(url: str, content_type: str | None = None) -> MediaType:
    normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized_content_type.startswith("image/"):
        return "image"
    if normalized_content_type.startswith("video/"):
        return "video"

    suffix = PurePosixPath(unquote(urlparse(url).path)).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"

    if normalized_content_type:
        raise ToolError(
            f"URL is not an image or video (Content-Type: {normalized_content_type})"
        )
    raise ToolError(
        "Could not determine whether the URL is an image or video; "
        "the server did not provide a usable Content-Type and the URL has no recognized media suffix"
    )


async def detect_media_type(client: httpx.AsyncClient, url: str) -> MediaType:
    content_type: str | None = None
    try:
        response = await client.head(url, follow_redirects=True)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type")
    except httpx.HTTPError:
        # Some object stores reject HEAD requests. The URL suffix still provides
        # a reliable fallback for ordinary image and video links.
        pass

    return classify_media_type(url, content_type)


def build_vllm_payload(prompt: str, media_url: str, media_type: MediaType = "image") -> dict[str, Any]:
    app_config = get_app_config()
    media_key = f"{media_type}_url"
    return {
        "model": app_config.vllm.model,
        "temperature": app_config.default_temperature,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": app_config.system_prompt,
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt,
                    },
                    {
                        "type": media_key,
                        media_key: {
                            "url": media_url,
                        },
                    },
                ],
            },
        ],
    }


def extract_message_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ToolError("vLLM response did not contain choices")

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise ToolError("vLLM response did not contain a message")

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        if parts:
            return "\n".join(parts)

    raise ToolError("vLLM response did not contain readable text content")


async def analyze_engineering_scene(prompt: str, media_url: str) -> AnalyzeImageOutput:
    app_config = get_app_config()
    headers = {
        "Authorization": f"Bearer {app_config.vllm.api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=app_config.request_timeout_seconds, trust_env=False) as client:
            media_type = await detect_media_type(client, media_url)
            payload = build_vllm_payload(
                prompt=prompt,
                media_url=media_url,
                media_type=media_type,
            )
            response = await client.post(
                build_chat_completions_url(str(app_config.vllm.api_url)),
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text.strip() or str(exc)
        raise ToolError(f"vLLM request failed with status {exc.response.status_code}: {detail}") from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"vLLM request failed: {exc}") from exc

    return AnalyzeImageOutput(result=extract_message_text(response.json()))
