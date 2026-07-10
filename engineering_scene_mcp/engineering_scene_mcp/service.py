from __future__ import annotations

from typing import Any

import httpx
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, HttpUrl

from engineering_scene_mcp.config import get_app_config, get_settings


class AnalyzeImageInput(BaseModel):
    prompt: str = Field(min_length=1, description="User prompt for the engineering scene analysis.")
    image_url: HttpUrl = Field(description="Publicly reachable image URL for the model to inspect.")


class AnalyzeImageOutput(BaseModel):
    result: str = Field(description="Model response returned from the multimodal vLLM backend.")


def build_chat_completions_url(api_url: str) -> str:
    return f"{api_url.rstrip('/')}/chat/completions"


def build_vllm_payload(prompt: str, image_url: str) -> dict[str, Any]:
    app_config = get_app_config()
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
                        "type": "image_url",
                        "image_url": {
                            "url": image_url,
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


async def analyze_engineering_scene(prompt: str, image_url: str) -> AnalyzeImageOutput:
    app_config = get_app_config()
    headers = {
        "Authorization": f"Bearer {app_config.vllm.api_key}",
        "Content-Type": "application/json",
    }
    payload = build_vllm_payload(prompt=prompt, image_url=image_url)

    try:
        async with httpx.AsyncClient(timeout=app_config.request_timeout_seconds, trust_env=False) as client:
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
