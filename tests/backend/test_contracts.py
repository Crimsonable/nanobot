import asyncio

import pytest
from pydantic import ValidationError

from agent_backend.journal import Journal
from agent_backend.models import Attachment, Envelope, Identity
from agent_backend.wire import Peer
from container_up.session_results import final_message
from bucket_runtime.sandbox_executor import contained


def test_identity_and_filename_boundaries():
    assert Identity(frontend_id="a/b", user_id="c").identity_id != Identity(frontend_id="a-b", user_id="c").identity_id
    for name in ["../x", "a/b", "a\\b", "..", "bad\n.txt"]:
        with pytest.raises(ValidationError):
            Attachment(filename=name)
    with pytest.raises(ValidationError):
        Identity(frontend_id="f", user_id="u", admin=True)


def test_journal_atomic_terminal_and_outbox(tmp_path):
    db = Journal(tmp_path / "state.db")
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.put("run", "a", {"status": "completed"})
            db.enqueue("bucket", Envelope(type="request.completed", bucket_id="bucket"))
            raise RuntimeError("crash")
    assert db.get("run", "a") is None
    assert db.pending("bucket") == []
    db.enqueue("bucket", Envelope(type="request.completed", bucket_id="bucket"))
    first = db.pending("bucket")[0]
    db.close()
    db = Journal(tmp_path / "state.db")
    assert db.pending("bucket")[0] == first
    db.ack("other", first.seq, first.event_id)
    db.ack("bucket", first.seq, "wrong")
    assert db.pending("bucket")
    db.ack("bucket", first.seq, first.event_id)
    assert not db.pending("bucket")
    db.close()


def test_final_requires_correlated_finished_answer():
    def answer(message_id, parent, finish, parts, completed=1):
        return {"info": {"id": message_id, "role": "assistant", "parentID": parent,
                         "finish": finish, "time": {"created": 1, "completed": completed}}, "parts": parts}
    unrelated = answer("a", "other", "stop", [{"type": "text", "text": "wrong"}])
    assert final_message([unrelated], "input") is None
    summary = answer("summary", "input", "stop", [{"type": "text", "text": "internal summary"}])
    summary["info"]["summary"] = True
    assert final_message([summary], "input") is None
    tool = answer("b", "input", "tool-calls", [{"type": "tool", "state": {"status": "completed"}}])
    assert final_message([unrelated, tool], "input") is None
    final = answer("c", "input", "stop", [{"type": "text", "text": "done"}])
    assert final_message([unrelated, tool, final], "input") == ("done", None)
    tool["parts"][0]["state"]["status"] = "running"
    assert final_message([tool, final], "input") is None


def test_path_rejects_escape_and_links(tmp_path):
    workspace = tmp_path / "user"
    workspace.mkdir()
    (workspace / "report.txt").write_text("ok")
    (tmp_path / "secret").write_text("private")
    assert contained("report.txt", workspace) == workspace / "report.txt"
    with pytest.raises(PermissionError):
        contained("../secret", workspace)
    try:
        (workspace / "link").symlink_to(tmp_path / "secret")
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(PermissionError):
        contained("link", workspace)


class QueueSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.other = None
        self.drop_ack = False

    async def send(self, data):
        if self.drop_ack and isinstance(data, str):
            import json
            if json.loads(data)["type"] == "ack":
                return
        await self.other.incoming.put(data)

    async def recv(self):
        return await self.incoming.get()


@pytest.mark.asyncio
async def test_session_controls_are_memory_only_and_errors_return_to_caller(tmp_path):
    a, b = QueueSocket(), QueueSocket()
    a.other, b.other = b, a
    db_a, db_b = Journal(tmp_path / "a.db"), Journal(tmp_path / "b.db")
    operations = []

    async def handler(event):
        raise AssertionError("session operations must not enter the durable handler")

    async def session_handler(payload):
        operations.append(payload["operation"])
        if payload["operation"] == "delete":
            raise RuntimeError("busy session")

    peer_a = Peer(a, db_a, "b", handler)
    peer_b = Peer(b, db_b, "b", handler, session_handler)
    tasks = [asyncio.create_task(peer_a.run()), asyncio.create_task(peer_b.run())]
    try:
        await peer_a.session_call({"session_id": "ses_one", "operation": "quiesce"})
        with pytest.raises(RuntimeError, match="busy session"):
            await peer_a.session_call({"session_id": "ses_one", "operation": "delete"})
        assert operations == ["quiesce", "delete"]
        assert not db_a.pending("b") and not db_b.pending("b")
        assert db_a.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
        assert db_b.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        db_a.close()
        db_b.close()


@pytest.mark.asyncio
async def test_durable_events_are_deduplicated(tmp_path):
    a, b = QueueSocket(), QueueSocket()
    a.other, b.other = b, a
    db_a, db_b = Journal(tmp_path / "a.db"), Journal(tmp_path / "b.db")
    delivered = []

    async def handler(event):
        delivered.append(event.event_id)

    peer_a, peer_b = Peer(a, db_a, "b", handler), Peer(b, db_b, "b", handler)
    tasks = [asyncio.create_task(peer_a.run()), asyncio.create_task(peer_b.run())]
    try:
        event = Envelope(type="request.submit", bucket_id="b", seq=12)
        await peer_a.send(event)
        await peer_a.send(event)
        await asyncio.sleep(0.05)
        assert delivered == [event.event_id]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        db_a.close()
        db_b.close()


@pytest.mark.asyncio
async def test_reconnect_replays_unacked_event_without_reexecution(tmp_path):
    db_a, db_b = Journal(tmp_path / "a.db"), Journal(tmp_path / "b.db")
    delivered = []

    async def handler(event):
        delivered.append(event.event_id)

    db_a.enqueue("b", Envelope(type="request.submit", bucket_id="b", request_id="r"))
    for drop_ack in [True, False]:
        a, b = QueueSocket(), QueueSocket()
        a.other, b.other, b.drop_ack = b, a, drop_ack
        peers = [Peer(a, db_a, "b", handler), Peer(b, db_b, "b", handler)]
        tasks = [asyncio.create_task(peer.run()) for peer in peers]
        try:
            await asyncio.sleep(0.1)
            assert len(delivered) == 1
            assert bool(db_a.pending("b")) == drop_ack
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    db_a.close()
    db_b.close()


@pytest.mark.asyncio
async def test_outbox_notifications_drain_multiple_batches_without_waiting_for_ack(tmp_path):
    db = Journal(tmp_path / "sender.db")
    sent = asyncio.Queue()

    class Socket:
        async def send(self, data):
            sent.put_nowait(Envelope.model_validate_json(data))

    async def unused(event):
        raise AssertionError("pump does not receive")

    for index in range(300):
        db.enqueue("b", Envelope(type="opencode.event", bucket_id="b", request_id=str(index)))
    pump = asyncio.create_task(Peer(Socket(), db, "b", unused).pump())
    try:
        async with asyncio.timeout(2):
            received = [await sent.get() for _ in range(300)]
            assert [e.request_id for e in received] == [str(i) for i in range(300)]
            assert len({e.seq for e in received}) == 300
            db.enqueue("b", Envelope(type="request.completed", bucket_id="b", request_id="next"))
            assert (await sent.get()).request_id == "next"
        # Unacknowledged events remain durable; no duplicate timer sends them on this connection.
        await asyncio.sleep(0)
        assert sent.empty()
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        db.close()
