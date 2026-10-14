import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from agent_backend.config import ControlConfig
from agent_backend.journal import Journal
from agent_backend.models import Envelope, Identity, Submit
from agent_backend.session_history import (
    database, delete_history, export_history, import_history, validate_destination,
)
from container_up.server import ControlService, Inbound
from support import register_bucket


def native_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript('''
            PRAGMA foreign_keys=ON;
            CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT);
            CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT REFERENCES project(id),
                parent_id TEXT, directory TEXT, metadata TEXT, permission TEXT, title TEXT);
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT REFERENCES session(id) ON DELETE CASCADE,
                data TEXT);
            CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT,
                message_id TEXT REFERENCES message(id) ON DELETE CASCADE, data TEXT);
            CREATE TABLE todo (session_id TEXT REFERENCES session(id) ON DELETE CASCADE, content TEXT);
        ''')


def seed(path, identity, chat="chat", session="ses_root", children=True):
    context = {**identity.model_dump(), "identity_id": identity.identity_id, "chat_id": chat,
               "bucket_id": "old", "requests": {"old-input": "old-request"}, "schema_version": 1}
    with database(path) as db:
        db.execute("INSERT OR IGNORE INTO project VALUES ('project', '/workspace')")
        for sid in [session, *([session + '_child'] if children else [])]:
            db.execute("INSERT INTO session VALUES (?, 'project', ?, '/workspace', ?, '[]', 'Conversation')",
                       (sid, None if sid == session else session, json.dumps({"bucket_runtime": context})))
            db.execute("INSERT INTO message VALUES (?, ?, ?)", (sid + '_msg', sid,
                       json.dumps({"role": "assistant", "finish": "stop", "summary": True})))
            db.execute("INSERT INTO part VALUES (?, ?, ?, ?)", (sid + '_part', sid, sid + '_msg',
                       json.dumps({"type": "tool", "callID": "call", "state": {"output": "kept history"}})))
            db.execute("INSERT INTO todo VALUES (?, 'keep todo')", (sid,))


def test_round_trip_preserves_native_history_and_replaces_only_its_own_rows(tmp_path):
    source, target = tmp_path / "a.db", tmp_path / "b.db"
    for path in [source, target]:
        native_db(path)
    identity = Identity(frontend_id="web", user_id="alice")
    seed(source, identity)
    seed(source, Identity(frontend_id="web", user_id="bob"), session="bob", children=False)
    history = export_history(source, "ses_root", identity, "chat")
    validate_destination(target, history)
    delete_history(source, history)
    import_history(target, history, identity, "chat", "b")
    with database(source) as db:
        assert [r[0] for r in db.execute("SELECT id FROM session")] == ["bob"]
    with database(target) as db:
        db.execute("UPDATE part SET data=? WHERE id='ses_root_part'", (json.dumps({"text": "newest"}),))
    latest = export_history(target, "ses_root", identity, "chat")
    delete_history(target, latest)
    import_history(source, latest, identity, "chat", "a")
    # Retrying after a successful commit is harmless and replaces stale data.
    import_history(source, latest, identity, "chat", "a")
    restored = export_history(source, "ses_root", identity, "chat")
    assert restored.rows["part"] == latest.rows["part"]
    assert restored.rows["todo"] == latest.rows["todo"]
    assert len(restored.rows["session"]) == 2
    context = json.loads(restored.rows["session"][0]["metadata"])["bucket_runtime"]
    assert context["bucket_id"] == "a" and context["requests"] == {}


def test_import_failure_is_atomic_and_foreign_ownership_is_rejected(tmp_path):
    source, target = tmp_path / "a.db", tmp_path / "b.db"
    for path in [source, target]:
        native_db(path)
    identity = Identity(frontend_id="web", user_id="alice")
    seed(source, identity)
    with pytest.raises(PermissionError):
        export_history(source, "ses_root", Identity(frontend_id="other", user_id="alice"), "chat")
    history = export_history(source, "ses_root", identity, "chat")
    with database(target) as db:
        db.execute("CREATE TRIGGER fail_part BEFORE INSERT ON part BEGIN SELECT RAISE(ABORT, 'fail'); END")
    with pytest.raises(sqlite3.IntegrityError):
        import_history(target, history, identity, "chat", "b")
    with database(target) as db:
        assert db.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_part")
    seed(target, Identity(frontend_id="web", user_id="bob"))
    with pytest.raises(PermissionError):
        import_history(target, history, identity, "chat", "b")


@pytest.fixture
def migration(tmp_path):
    service = ControlService(ControlConfig(state_root=tmp_path / "control",
                                          frontends={"web": {"provider": "web"}}))
    register_bucket(service, "a")
    register_bucket(service, "b")
    service.config.runtime_templates["web"].state_root = tmp_path / "runtime"
    identity = Identity(frontend_id="web", user_id="alice")
    submit = Submit(request_id="next", identity=identity, chat_id="chat", content="continue",
                    deadline_at=time.time() + 60)
    paths = {}
    for bucket in ["a", "b"]:
        root = service.sessions.root(identity, bucket)
        paths[bucket] = root / "opencode-home/data/opencode/opencode.db"
        native_db(paths[bucket])
    seed(paths["a"], identity)
    journal = Journal(service.sessions.root(identity, "a") / "runtime.sqlite")
    journal.put("session", submit.session_key, {"session_id": "ses_root", "identity_id": identity.identity_id})
    journal.close()
    bucket = service.db.get("bucket", "a")
    service.db.put("bucket", "a", {**bucket, "status": "idle"})
    service.peers.pop("a")
    yield service, submit, paths
    service.db.close()


@pytest.mark.asyncio
async def test_offline_source_is_discovered_and_moved_without_starting_it(migration):
    service, submit, paths = migration
    row = await service.submit("web", Inbound(user_id="alice", chat_id="chat", content="continue"))
    assert row["bucket_id"] == "b" and row["submit"]["session_id"] == "ses_root"
    assert service.db.get("session", submit.session_key)["bucket_id"] == "b"
    with database(paths["a"]) as db:
        assert db.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 0
    journal = Journal(service.sessions.root(submit.identity, "a") / "runtime.sqlite")
    try:
        assert journal.get("session", submit.session_key) is None
    finally:
        journal.close()
    assert export_history(paths["b"], "ses_root", submit.identity, "chat").rows["part"]
    assert not service.sessions.histories


@pytest.mark.asyncio
async def test_online_source_is_quiesced_before_export_and_deleted_before_import(migration):
    service, submit, paths = migration
    bucket = service.db.get("bucket", "a")
    service.db.put("bucket", "a", {**bucket, "status": "running"})
    operations = []

    async def call(payload):
        operations.append(payload["operation"])
        if payload["operation"] == "quiesce":
            assert not service.sessions.histories
        else:
            history = service.sessions.histories[submit.session_key]
            assert history.rows["part"]
            with database(paths["b"]) as db:
                assert db.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 0
            delete_history(paths["a"], history)

    service.peers["a"] = SimpleNamespace(closed=False, session_call=call)
    assert await service.sessions.prepare(submit, "b") == "ses_root"
    assert operations == ["quiesce", "delete"]


@pytest.mark.asyncio
async def test_import_retry_keeps_memory_user_lock_and_survives_caller_disconnect(migration, monkeypatch):
    service, submit, paths = migration
    import container_up.session_manager as module
    original = module.import_history
    failed = asyncio.Event()
    attempts = 0

    def flaky(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            with database(paths["a"]) as db:
                assert db.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 0
            assert service.user_locks[submit.identity.identity_id].locked()
            assert service.sessions.histories[submit.session_key].rows["message"]
            failed.set()
            raise sqlite3.OperationalError("temporarily locked")
        return original(*args)

    monkeypatch.setattr(module, "import_history", flaky)
    caller = asyncio.create_task(service.submit("web", Inbound(user_id="alice", chat_id="chat",
                                                                content="continue", request_id="retry")))
    await asyncio.wait_for(failed.wait(), 3)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.wait_for(asyncio.gather(*service.submission_tasks), 5)
    assert attempts == 2
    assert service.db.get("request", "retry")["submit"]["session_id"] == "ses_root"
    assert not service.sessions.histories


@pytest.mark.asyncio
async def test_late_started_event_cannot_move_catalog_back(migration):
    service, submit, paths = migration
    row = await service.submit("web", Inbound(user_id="alice", chat_id="chat", content="continue"))
    await service.on_event("b", Envelope(type="request.completed", bucket_id="b", request_id=row["request_id"],
        payload={"request_id": row["request_id"], "status": "completed"}))
    catalog = service.db.get("session", submit.session_key)
    service.db.put("session", submit.session_key, {**catalog, "bucket_id": "a"})
    await service.on_event("b", Envelope(type="request.started", bucket_id="b", request_id=row["request_id"],
                                        payload={"session_id": "ses_root"}))
    assert service.db.get("session", submit.session_key)["bucket_id"] == "a"
