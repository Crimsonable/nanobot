from contextlib import closing
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_backend.journal import Journal
from container_up.migrations import upgrade_native_owner, upgrade_session_bindings


@pytest.mark.parametrize("pending", ["request", "migration", "outbox"])
def test_upgrade_refuses_unfinished_old_work(tmp_path, pending):
    with closing(Journal(tmp_path / "state.sqlite")) as db:
        db.put("bucket", "old", {"bucket_id": "old", "status": "running"})
        db.db.execute("CREATE TABLE outbox (value TEXT)")
        if pending == "outbox":
            db.db.execute("INSERT INTO outbox VALUES ('pending')")
        else:
            db.put(pending, "pending", {"request_id": "pending"})
        with pytest.raises(RuntimeError):
            upgrade_session_bindings(db)
        assert "password" not in db.get("bucket", "old")


def test_drained_upgrade_preserves_history_and_is_idempotent(tmp_path):
    with closing(Journal(tmp_path / "state.sqlite")) as db:
        db.put("bucket", "old", {"bucket_id": "old", "status": "running"})
        db.put("request", "r", {"request_id": "r", "result": {"status": "completed"}})
        db.put("session", "chat", {"bucket_id": "old", "session_id": "ses_original"})
        db.db.execute("CREATE TABLE outbox (value TEXT)")
        upgrade_session_bindings(db)
        bucket = db.get("bucket", "old")
        assert bucket["status"] == "idle" and len(bucket["password"]) >= 32
        assert db.get("request", "r")["output_collected"]
        assert db.get("session", "chat")["session_id"] == "ses_original"
        assert not db.db.execute("SELECT 1 FROM sqlite_master WHERE name='outbox'").fetchone()
        upgrade_session_bindings(db)
        assert db.get("bucket", "old") == bucket


@pytest.mark.asyncio
async def test_old_native_metadata_is_converted_through_native_api():
    owner = {"frontend_id": "web", "user_id": "alice", "chat_id": "chat", "identity_id": "identity"}
    client = AsyncMock()
    client.session.return_value = {"metadata": {"bucket_runtime": owner, "keep": "value"}}
    assert await upgrade_native_owner(client, "ses_old", owner)
    client.call.assert_awaited_once_with(
        "PATCH",
        "/api/session/ses_old",
        json={
            "metadata": {"managed": owner, "keep": "value"},
            "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
        },
    )
    client.call.reset_mock()
    client.session.return_value = {"metadata": {"bucket_runtime": {**owner, "user_id": "other"}}}
    with pytest.raises(PermissionError):
        await upgrade_native_owner(client, "ses_old", owner)
    client.call.assert_not_called()
    client.session.side_effect = httpx.HTTPStatusError(
        "missing", request=httpx.Request("GET", "http://native"), response=httpx.Response(404)
    )
    assert not await upgrade_native_owner(client, "ses_old", owner)
