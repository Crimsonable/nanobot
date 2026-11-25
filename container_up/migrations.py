"""One-time upgrades for persisted CU state, outside request scheduling."""
import secrets

from agent_backend.journal import Journal
from agent_backend.models import Submit


def upgrade_session_bindings(db: Journal) -> None:
    old_buckets = [row for row in db.records("bucket") if "password" not in row]
    if old_buckets:
        if any(not row.get("result") for row in db.records("request")) or db.records("migration"):
            raise RuntimeError("Finish old requests and migrations before the native-bucket upgrade")
        if db.db.execute("SELECT 1 FROM sqlite_master WHERE name='outbox'").fetchone():
            if db.db.execute("SELECT 1 FROM outbox LIMIT 1").fetchone():
                raise RuntimeError("Drain old control commands before the native-bucket upgrade")
        with db.transaction():
            for row in old_buckets:
                db.put("bucket", row["bucket_id"], {**row, "status": "idle", "opencode": None,
                    "password": secrets.token_urlsafe(32)})
            for row in db.records("request"):
                db.put("request", row["request_id"], {**row, "output_collected": True})
            db.db.execute("DROP TABLE IF EXISTS outbox")
    legacy = {b["identity_id"]: b for b in db.records("binding") if "session_key" not in b}
    if not legacy:
        return
    with db.transaction():
        for identity_id in legacy:
            db.delete("binding", identity_id)
        for row in db.records("request"):
            if row.get("result"):
                continue
            submit = Submit.model_validate(row["submit"])
            if binding := legacy.get(submit.identity.identity_id):
                db.put("binding", submit.session_key, {**binding, "bucket_id": row["bucket_id"],
                    "session_key": submit.session_key, "chat_id": submit.chat_id})


async def upgrade_native_owner(client, session_id, owner):
    """Convert metadata on existing native transcripts; never read their database."""
    import httpx
    try:
        info = await client.session(session_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return False
        raise
    metadata = info.get("metadata", {})
    if "managed" in metadata:
        return True
    previous = metadata.get("bucket_runtime", {})
    if any(previous.get(key) != value for key, value in owner.items()):
        raise PermissionError("existing native session ownership mismatch")
    await client.call("PATCH", f"/api/session/{session_id}", json={
        "metadata": {**{key: value for key, value in metadata.items() if key != "bucket_runtime"}, "managed": owner},
        "permissions": [{"action": "*", "resource": "*", "effect": "allow"}]})
    return True
