"""Channel adapters are called only by the control service, never by a bucket."""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from agent_backend.config import ControlConfig
from agent_backend.models import Result


class ChannelService:
    def __init__(self, config: ControlConfig):
        self.config = config
        self.parsers: dict = {}

    async def start(self, dispatch: Callable[[str, dict], Awaitable[None]]) -> None:
        import asyncio

        from container_up.http_state import init_dispatch_session
        init_dispatch_session()
        for frontend_id, frontend in self.config.frontends.items():
            raw = dict(frontend.channel_config)

            async def receive(payload: dict, frontend_id: str = frontend_id) -> None:
                await dispatch(frontend_id, payload)

            if frontend.provider == "qxt":
                from container_up.qxt_im_tool import QxtIMParser
                parser = QxtIMParser(frontend_id=frontend_id, frontend_config=raw)
            elif frontend.provider == "feishu":
                from container_up.feishu_im_tool import FeishuIMParser
                parser = FeishuIMParser(frontend_id=frontend_id, frontend_config=raw,
                    dispatch_event=receive, main_loop=asyncio.get_running_loop())
            else:
                from container_up.web_im_tool import WebIMParser
                parser = WebIMParser(frontend_id=frontend_id, frontend_config=raw)
            self.parsers[frontend_id] = parser
            parser.start()

    async def deliver(self, record: dict, result: Result) -> None:
        identity = record["submit"]["identity"]
        # Routing metadata comes from the inbound request, never the model.
        metadata = {**record["metadata"], "frontend_id": identity["frontend_id"],
                    "user_id": identity["user_id"], "request_id": result.request_id}
        if result.usage:
            metadata["usage"] = result.usage.model_dump()
        content = result.content
        attachments = [a.model_dump(mode="json") for a in result.attachments]
        frontend = self.config.frontends[identity["frontend_id"]]
        if frontend.provider != "web":
            links = "\n".join(f"{a.filename}: {a.url}" for a in result.attachments)
            content = "\n\n".join(part for part in [content, links] if part)
            attachments = []  # Deliver object URLs without downloading/re-uploading channel files.
        await self.parsers[identity["frontend_id"]].post_message(payload={
            "chat_id": record["chat_id"], "content": content,
            "attachments": attachments,
            "metadata": metadata,
        })

    async def stop(self) -> None:
        from container_up.http_state import close_dispatch_session
        for parser in self.parsers.values():
            parser.stop()
        await close_dispatch_session()
