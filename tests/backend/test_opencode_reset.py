import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_backend.config import ControlConfig, RuntimeConfig
from agent_backend.models import Envelope, Identity
from bucket_runtime.adapter import BucketRuntimeAdapter
from bucket_runtime.opencode_server import OpenCodeServer
from container_up.server import create_app
from tests.backend.support import register_bucket


class Process:
    def __init__(self):
        self.exited = asyncio.Event()

    @property
    def returncode(self):
        return 0 if self.exited.is_set() else None

    async def wait(self):
        await self.exited.wait()
        return 0


@pytest.fixture
async def server(tmp_path):
    (tmp_path / "common/web/app").mkdir(parents=True)
    config_file = tmp_path / "opencode.json"
    config_file.write_text(json.dumps({"model": "provider/old"}))
    config = RuntimeConfig(frontend_id="web", bucket_id="b", common_root=tmp_path / "common",
        state_root=tmp_path / "state", workspace_root=tmp_path / "users",
        opencode={"config_file": config_file, "context_root": tmp_path / "context"})
    opencode = OpenCodeServer(config)
    runtime = BucketRuntimeAdapter(config, opencode)

    async def start(raw):
        opencode.process = Process()

    async def stop():
        if opencode.process:
            opencode.process.exited.set()

    def handle(request):
        opencode.plugin_ready.set()
        return httpx.Response(200, json={})

    async def events():
        process = opencode.process
        yield {"payload": {"type": "server.connected"}}
        await process.exited.wait()

    opencode._start_process = AsyncMock(side_effect=start)
    opencode._stop_process = AsyncMock(side_effect=stop)
    opencode.events = events
    await opencode.http.aclose()
    opencode.http = httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle))
    await opencode.start(runtime.on_opencode_event)
    runtime.ready = True
    try:
        yield runtime
    finally:
        await runtime.shutdown()
        await opencode.close()
        runtime.db.close()


@pytest.mark.asyncio
async def test_reset_reloads_config_preserves_state_and_process_watch(server):
    runtime, opencode = server, server.opencode
    runtime.config.opencode.config_file.write_text('{"model":"provider/new"}')
    runtime.db.put("session", "chat", {"session_id": "ses_existing"})
    old = opencode.process
    watch = asyncio.create_task(server.opencode.watch())
    await asyncio.sleep(0)
    try:
        await server.control({"operation": "reset_opencode"})
        await asyncio.sleep(0)
        assert old.exited.is_set() and opencode.process is not old
        assert opencode.active_config == {"model": "provider/new"}
        assert runtime.db.get("session", "chat") == {"session_id": "ses_existing"}
        assert runtime.ready and runtime.opencode.plugin_ready.is_set() and runtime.accepting()
        assert runtime.opencode.connected.is_set()
        assert not watch.done()
        opencode.process.exited.set()
        with pytest.raises(RuntimeError, match="unexpectedly"):
            await asyncio.wait_for(watch, 1)
    finally:
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)
        await server.opencode.close()


@pytest.mark.asyncio
async def test_invalid_config_keeps_running_process(server):
    server.config.opencode.config_file.write_text("invalid json")
    with pytest.raises(RuntimeError, match="could not be loaded"):
        await server.reset_opencode()
    server.opencode._stop_process.assert_not_called()
    assert server.ready and server.accepting()


@pytest.mark.asyncio
async def test_active_or_queued_work_is_not_interrupted(server):
    server.db.put("run", "r", {"status": "queued"})
    with pytest.raises(RuntimeError, match="active requests"):
        await server.reset_opencode()
    server.opencode._stop_process.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_failed_start_rolls_back_or_stays_unavailable(server, rollback_fails):
    opencode, runtime = server.opencode, server
    original_start = opencode._start_process.side_effect
    runtime.config.opencode.config_file.write_text('{"model":"provider/new"}')

    async def start(raw):
        if raw["model"] == "provider/new" or rollback_fails:
            raise RuntimeError("native error containing a secret")
        await original_start(raw)

    opencode._start_process.side_effect = start
    try:
        with pytest.raises(RuntimeError, match="remains unavailable" if rollback_fails else "previous configuration restored") as exc:
            await server.reset_opencode()
        assert "secret" not in str(exc.value)
        assert runtime.ready is not rollback_fails
        assert runtime.accepting() is not rollback_fails
        if rollback_fails:
            # The runtime remains alive so a corrected config can be retried.
            opencode._start_process.side_effect = original_start
            await server.reset_opencode()
            assert runtime.ready and runtime.accepting()
    finally:
        await server.opencode.close()


@pytest.mark.asyncio
async def test_disconnect_does_not_cancel_reset_and_duplicates_are_rejected(server):
    entered, release = asyncio.Event(), asyncio.Event()
    original_start = server.opencode._start_process.side_effect

    async def start(raw):
        entered.set()
        await release.wait()
        await original_start(raw)

    server.opencode._start_process.side_effect = start
    caller = asyncio.create_task(server.control({"operation": "reset_opencode"}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not server.ready and not server.accepting()
        with pytest.raises(RuntimeError, match="already in progress"):
            await server.control({"operation": "reset_opencode"})
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        release.set()
        await asyncio.wait_for(server._reset_task, 1)
        assert server.ready
    finally:
        release.set()
        await server.opencode.close()


@pytest.fixture
def control(tmp_path):
    app = create_app(ControlConfig(state_root=tmp_path / "control",
        frontends={"web": {"provider": "web", "channel_config": {}}}))
    register_bucket(app.state.service)
    app.state.service.peers["b"].session_call = AsyncMock()
    yield app
    app.state.service.db.close()


@pytest.mark.asyncio
async def test_reset_http_statuses(control):
    service = control.state.service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=control), base_url="http://test") as client:
        path = "/internal/frontends/web/opencode/reset"
        assert (await client.post("/internal/frontends/missing/opencode/reset")).status_code == 404
        result = await client.post(path)
        assert result.status_code == 200
        assert result.json() == {"frontend_id": "web", "status": "ready",
                                 "buckets": [{"bucket_id": "b", "status": "ready"}], "skipped_buckets": []}
        assert service.peers["b"].session_call.call_args.args[0]["operation"] == "reset_opencode"
        service.pool.pins["b"] = 1
        result = await client.post(path)
        assert result.status_code == 207 and result.json()["buckets"][0]["status_code"] == 409
        service.pool.pins["b"] = 0
        service.db.enqueue("b", Envelope(type="request.submit", bucket_id="b"))
        assert (await client.post(path)).json()["buckets"][0]["status_code"] == 409
        for event in service.db.pending("b"):
            service.db.ack("b", event.seq, event.event_id)
        service.peers["b"].session_call.side_effect = RuntimeError("previous configuration restored")
        assert (await client.post(path)).json()["buckets"][0]["status_code"] == 502
        service.peers["b"].closed = True
        assert (await client.post(path)).json()["buckets"][0]["status_code"] == 503
        assert not service.pool.resetting and service.pool.pins["b"] == 0


@pytest.mark.asyncio
async def test_reset_excludes_placement_and_waits_before_session_migration(control):
    service = control.state.service
    entered, release = asyncio.Event(), asyncio.Event()

    async def reset(payload):
        entered.set()
        await release.wait()

    service.peers["b"].session_call.side_effect = reset
    caller = asyncio.create_task(service.reset_frontend_opencode("web"))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        await service.on_event("b", Envelope(type="bucket.status", bucket_id="b", payload={"accepting": True}))
        assigned = service.pool.reserve(Identity(frontend_id="web", user_id="new-user"))
        assert assigned["bucket_id"] != "b"
        assert service.pool.busy("b")
        migration = asyncio.create_task(service.pool.ready("b"))
        await asyncio.sleep(0)
        assert not migration.done()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        pending = list(service.reset_tasks)
        release.set()
        await asyncio.wait_for(asyncio.gather(*pending, migration), 1)
        assert not service.pool.resetting and not service.pool.busy("b")
    finally:
        release.set()
        await asyncio.gather(*service.reset_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_frontend_batch_isolates_other_frontends_and_reports_partial_failure(control):
    service = control.state.service
    for bucket_id, frontend in [("second", "web"), ("busy", "web"), ("sleeping", "web"), ("other", "other")]:
        register_bucket(service, bucket_id, frontend)
        service.peers[bucket_id].session_call = AsyncMock()
    service.db.put("bucket", "sleeping", {**service.db.get("bucket", "sleeping"), "status": "idle"})
    service.pool.pins["busy"] = 1
    result = await service.reset_frontend_opencode("web")
    assert result["status"] == "partial_failure"
    outcomes = {row["bucket_id"]: row for row in result["buckets"]}
    assert outcomes["b"]["status"] == outcomes["second"]["status"] == "ready"
    assert outcomes["busy"]["status_code"] == 409
    assert result["skipped_buckets"] == ["sleeping"]
    service.peers["other"].session_call.assert_not_called()
    service.peers["sleeping"].session_call.assert_not_called()
    service.peers["busy"].session_call.assert_not_called()


@pytest.mark.asyncio
async def test_frontend_with_only_idle_buckets_needs_no_restart(control):
    service = control.state.service
    service.db.put("bucket", "b", {**service.db.get("bucket", "b"), "status": "idle"})
    result = await service.reset_frontend_opencode("web")
    assert result == {"frontend_id": "web", "status": "no_running_buckets",
                      "buckets": [], "skipped_buckets": ["b"]}
    service.peers["b"].session_call.assert_not_called()


@pytest.mark.asyncio
async def test_frontend_endpoint_resets_through_control_protocol(control, server):
    from agent_backend.wire import Peer
    from tests.backend.test_contracts import QueueSocket

    service = control.state.service
    left, right = QueueSocket(), QueueSocket()
    left.other, right.other = right, left
    cu_peer = Peer(left, service.db, "b", lambda event: service.on_event("b", event))
    runtime_peer = Peer(right, server.db, "b", server.on_command, server.control)
    service.peers["b"] = cu_peer
    server.peer = runtime_peer
    server.config.opencode.config_file.write_text('{"model":"provider/updated"}')
    original_process = server.opencode.process
    tasks = [asyncio.create_task(peer.run()) for peer in (cu_peer, runtime_peer)]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=control), base_url="http://test") as client:
            response = await asyncio.wait_for(client.post("/internal/frontends/web/opencode/reset"), 2)
        assert response.status_code == 200 and response.json()["status"] == "ready"
        assert server.opencode.process is not original_process
        assert server.opencode.active_config["model"] == "provider/updated"
        assert not cu_peer.closed and not runtime_peer.closed
        assert server.ready
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await server.opencode.close()


@pytest.mark.asyncio
async def test_stopping_only_the_child_keeps_http_transport_open(tmp_path):
    opencode = OpenCodeServer(RuntimeConfig(frontend_id="web", bucket_id="b", state_root=tmp_path))
    await opencode.stop()
    assert not opencode.http.is_closed
    await opencode.close()
    assert opencode.http.is_closed


@pytest.mark.asyncio
async def test_start_does_not_launch_a_second_process(server):
    opencode = server.opencode
    original = opencode.process
    with pytest.raises(RuntimeError, match="already running"):
        await opencode.start(server.on_opencode_event)
    assert opencode.process is original and original.returncode is None
    assert opencode.ready
    opencode._start_process.assert_awaited_once()


@pytest.mark.asyncio
async def test_plugin_initialization_failure_restores_last_fully_ready_config(server):
    opencode = server.opencode
    server.config.opencode.config_file.write_text('{"model":"provider/new"}')
    original_start = opencode._start_process.side_effect
    attempted = []

    async def start(raw):
        attempted.append(raw["model"])
        await original_start(raw)

    def handle(request):
        if attempted[-1] == "provider/new":
            return httpx.Response(503)
        opencode.plugin_ready.set()
        return httpx.Response(200)

    opencode._start_process.side_effect = start
    await opencode.http.aclose()
    opencode.http = httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle))
    with pytest.raises(RuntimeError, match="previous configuration restored"):
        await server.reset_opencode()
    assert attempted == ["provider/new", "provider/old"]
    assert opencode.active_config == {"model": "provider/old"}
    assert opencode.ready and server.ready and server.accepting()


@pytest.mark.asyncio
async def test_shutdown_during_rollback_stops_the_replacement_process(server):
    opencode = server.opencode
    server.config.opencode.config_file.write_text('{"model":"provider/new"}')
    original_start = opencode._start_process.side_effect
    rollback_started = asyncio.Event()

    async def start(raw):
        if raw["model"] == "provider/new":
            raise RuntimeError("invalid model config")
        await original_start(raw)
        rollback_started.set()
        await asyncio.Event().wait()

    opencode._start_process.side_effect = start
    caller = asyncio.create_task(server.control({"operation": "reset_opencode"}))
    try:
        await asyncio.wait_for(rollback_started.wait(), 1)
        await server.shutdown()
        assert opencode.process.returncode == 0
        assert not server.ready and not opencode.ready and not opencode.connected.is_set()
        assert opencode.subscription is None
    finally:
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
