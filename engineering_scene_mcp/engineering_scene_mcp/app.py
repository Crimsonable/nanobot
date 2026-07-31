from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from engineering_scene_mcp.config import get_app_config, get_settings
from engineering_scene_mcp.service import AnalyzeImageInput, AnalyzeImageOutput, analyze_engineering_scene


class HealthCheckAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return "GET /health/live" not in message and "GET /health/ready" not in message


settings = get_settings()
app_config = get_app_config()

mcp = FastMCP(
    app_config.service_name,
    instructions=app_config.system_prompt,
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=app_config.transport_security.enable_dns_rebinding_protection,
        allowed_hosts=app_config.transport_security.allowed_hosts,
        allowed_origins=app_config.transport_security.allowed_origins,
    ),
)


@mcp.tool(
    name=app_config.tool_name,
    description="Analyze an engineering scene image or video with a user prompt and a media URL.",
)
async def analyze_engineering_scene_image(
    prompt: str,
    media_url: str,
) -> AnalyzeImageOutput:
    AnalyzeImageInput(prompt=prompt, media_url=media_url)
    return await analyze_engineering_scene(prompt=prompt, media_url=media_url)


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title=app_config.service_name,
    version="0.1.0",
    lifespan=lifespan,
)
app.mount("/mcp", mcp.streamable_http_app())


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready() -> dict[str, str]:
    return {"status": "ready"}


def main() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.addFilter(HealthCheckAccessFilter())

    uvicorn.run(
        "engineering_scene_mcp.app:app",
        host=app_config.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
