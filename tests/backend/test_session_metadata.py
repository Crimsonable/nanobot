import json

import httpx
import pytest

from agent_backend.config import OpenCodeConfig
from agent_backend.journal import Journal
from agent_backend.models import Envelope, Identity, Submit, digest
from bucket_runtime.opencode_client import OpenCodeClient


@pytest.mark.asyncio
async def test_create_and_reuse_metadata_preserves_identity_and_never_patches_permissions():
    context = {"schema_version": 1, "frontend_id": "web", "user_id": "alice", "identity_id": "identity",
               "chat_id": "group-1", "bucket_id": "b", "input_message_id": "m1",
               "requests": {"m1": "r1"}}
    info = {"id": "ses_generated", "directory": str(OpenCodeConfig().context_root.resolve()),
            "permission": [], "metadata": {"other_plugin": "keep"}}
    calls = []

    def handle(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "POST":
            info["metadata"].update(body["metadata"])
        if request.method == "PATCH":
            assert set(body) == {"metadata"}
            info["metadata"] = body["metadata"]
        return httpx.Response(200, json=info)

    client = OpenCodeClient(OpenCodeConfig(), "password")
    await client.http.aclose()
    async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle)) as client.http:
        assert await client.create_session([], context) == "ses_generated"
        next_context = {**context, "input_message_id": "m2", "requests": {"m2": "r2"}}
        await client.set_context("ses_generated", next_context)
        assert info["metadata"]["other_plugin"] == "keep"
        assert info["metadata"]["bucket_runtime"]["requests"] == {"m1": "r1", "m2": "r2"}
        with pytest.raises(PermissionError, match="ownership mismatch"):
            await client.set_context("ses_generated", {**next_context, "user_id": "bob"})
        with pytest.raises(PermissionError, match="ownership mismatch"):
            await client.set_context("ses_generated", {**next_context, "chat_id": "group-2"})
        # Upgrade an existing session without changing the native session ID.
        previous = info["metadata"]["bucket_runtime"]
        previous["conversation_id"] = digest([previous["identity_id"], previous.pop("chat_id")])
        with pytest.raises(PermissionError, match="ownership mismatch"):
            await client.set_context("ses_generated", {**next_context, "chat_id": "group-2"})
        await client.set_context("ses_generated", next_context)
        assert info["metadata"]["bucket_runtime"]["chat_id"] == "group-1"
        assert "conversation_id" not in info["metadata"]["bucket_runtime"]
    assert len([call for call in calls if call[0] == "PATCH"]) == 2


def test_identifier_migration_preserves_keys_and_values(tmp_path):
    path = tmp_path / "state.db"
    db = Journal(path)
    db.put("binding", "identity-hash", {"user_key": "identity-hash", "bucket_id": "b"})
    db.put("session", "conversation-hash", {"user_key": "identity-hash", "profile": "p", "session_id": "ses_a"})
    db.put("artifact", "att_a", {"owner": "identity-hash", "attachment": {"url": "https://files/a"}})
    db.db.execute("PRAGMA user_version=0")
    db.close()
    db = Journal(path)
    try:
        assert db.get("binding", "identity-hash") == {"identity_id": "identity-hash", "bucket_id": "b"}
        assert db.get("session", "conversation-hash") == {
            "identity_id": "identity-hash", "profile_id": "p", "session_id": "ses_a"}
        assert db.get("artifact", "att_a")["identity_id"] == "identity-hash"
    finally:
        db.close()


def test_chat_migration_preserves_sessions_and_pending_delivery(tmp_path):
    path = tmp_path / "control.db"
    submit = Submit(request_id="r", chat_id="group-1", identity=Identity(frontend_id="web", user_id="alice"),
                    profile_id="p", content="hello", deadline_at=123)
    legacy = submit.model_dump()
    legacy["conversation_id"] = submit.session_key
    legacy.pop("chat_id")
    event = Envelope(type="request.submit", bucket_id="b", request_id="r", payload=legacy)
    db = Journal(path)
    db.put("request", "r", {"request_id": "r", "chat_id": "group-1", "submit": legacy, "hash": "unchanged"})
    db.put("session", submit.session_key, {"session_id": "ses_original"})
    db.enqueue("b", event)
    db.db.execute("PRAGMA user_version=1")
    db.close()
    db = Journal(path)
    try:
        assert db.get("request", "r")["submit"] == submit.model_dump()
        assert db.get("request", "r")["hash"] == "unchanged"
        pending = db.pending("b")[0]
        assert pending.payload == submit.model_dump()
        assert pending.event_id == event.event_id
        assert pending.seq == 1
        assert db.get("session", submit.session_key)["session_id"] == "ses_original"
        assert db.db.execute("PRAGMA user_version").fetchone()[0] == 2
        db.migrate_chats()  # Reopening/migrating again does not duplicate deliveries.
        assert len(db.pending("b")) == 1
    finally:
        db.close()


def test_legacy_run_key_is_read_only_compatibility_and_never_guesses_chat():
    submit = Submit(request_id="r", chat_id="group-1", identity=Identity(frontend_id="web", user_id="alice"),
                    profile_id="p", content="hello", deadline_at=123)
    legacy = submit.model_dump()
    legacy["conversation_id"] = submit.session_key
    legacy.pop("chat_id")
    restored = Submit.from_record(legacy)
    assert restored.session_key == submit.session_key
    assert not restored.has_chat_id
    assert "conversation_id" not in restored.model_dump()
    with pytest.raises(ValueError):
        Submit.model_validate(legacy)  # New wire submissions cannot send the deleted field.
    with pytest.raises(ValueError, match="chat key mismatch"):
        Submit.from_record({**legacy, "chat_id": "another-group"})
