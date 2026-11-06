from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_backend.config import ControlConfig
from agent_backend.models import Attachment, Result
from channel_gateway.channel_service import ChannelService


@pytest.fixture
def config(tmp_path):
    return ControlConfig.model_validate({"state_root": str(tmp_path / "state"),
         "frontends": {"qxt": {"provider": "qxt", "channel_config": {}}}})


@pytest.mark.asyncio
async def test_qxt_delivery_preserves_media_and_trusted_group_target(config):
    channels = ChannelService(config)
    send = AsyncMock()
    channels.parsers["qxt"] = SimpleNamespace(post_message=send)
    target = {"to_group_id": "group-1"}
    await channels.deliver({"submit": {"identity": {"frontend_id": "qxt", "user_id": "alice"}},
        "chat_id": "group-1", "metadata": {"reply_target": target}},
        Result(request_id="r", status="completed", content="report",
               attachments=[Attachment(storage="qxt", frontend_id="qxt", media_id="media-1", filename="report.pdf")]))
    payload = send.call_args.kwargs["payload"]
    assert payload["metadata"]["reply_target"] == target
    assert payload["attachments"][0]["media_id"] == "media-1"
    assert payload["metadata"]["provider"] == "qxt"
    assert payload["content"] == "report"
