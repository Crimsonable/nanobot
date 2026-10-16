import time

import pytest
from pydantic import ValidationError

from agent_backend.config import ControlConfig, FrontendSettings, RuntimeSettings
from agent_backend.models import Submit
from container_up.server import Inbound, create_app
from web_server.app import InboundRequest


@pytest.mark.parametrize("field,value", [("epoch", 1), ("previous_turns", []), ("profile_id", "old")])
def test_submit_rejects_removed_session_fields(field, value):
    with pytest.raises(ValidationError, match="Extra inputs"):
        Submit.model_validate({"request_id": "r", "chat_id": "chat", "identity": {
            "frontend_id": "web", "user_id": "alice"}, "content": "hello",
            "deadline_at": time.time() + 60, field: value})


@pytest.mark.parametrize("schema,values", [
    (ControlConfig, {"frontends": {}, "bucket_ids": ["b"]}),
    (ControlConfig, {"frontends": {}, "runtime_template": {}}),
    (RuntimeSettings, {"workspace_layout": "identity_hash"}),
    (FrontendSettings, {"frontend_id": "web", "provider": "web", "runtime": {}, "profiles": {}}),
    (FrontendSettings, {"id": "web", "provider": "web", "runtime": {}}),
    (FrontendSettings, {"frontend_id": "qxt", "provider": "qxt", "runtime": {},
                        "channel": {"attachment_delivery": "media"}}),
    (FrontendSettings, {"frontend_id": "feishu", "provider": "feishu", "runtime": {},
                        "channel": {"APP_ID": "legacy"}}),
])
def test_configuration_rejects_deleted_fields(schema, values):
    with pytest.raises(ValidationError):
        schema.model_validate(values)


@pytest.mark.parametrize("schema", [Inbound, InboundRequest])
def test_chat_id_defaults_when_absent_or_blank(schema):
    values = {"user_id": "alice", "content": "hello"}
    if schema is InboundRequest:
        values["frontend_id"] = "web"
    assert schema.model_validate(values).chat_id == "default"
    assert schema.model_validate({**values, "chat_id": ""}).chat_id == "default"
    assert schema.model_validate({**values, "chat_id": None}).chat_id == "default"


@pytest.mark.parametrize("schema", [Inbound, InboundRequest])
def test_unknown_inbound_fields_are_ignored(schema):
    values = {"user_id": "alice", "content": "hello", "chat_id": "c1",
              "raw": {"legacy": True}, "frontend_id": "web"}
    assert schema.model_validate(values).chat_id == "c1"


def test_only_canonical_cu_routes_remain(tmp_path):
    app = create_app(ControlConfig(state_root=tmp_path, frontends={}))
    try:
        paths = {route.path for route in app.routes}
        assert not any(path.startswith("/api/") for path in paths)
        assert "/internal/attachments/upload" not in paths
        assert {"/inbound/{frontend_id}", "/inbound-sync/{frontend_id}",
                "/requests/{frontend_id}/{request_id}"} <= paths
    finally:
        app.state.service.db.close()
