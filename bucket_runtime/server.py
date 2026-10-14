from __future__ import annotations

import asyncio
import logging
import secrets

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from websockets.asyncio.client import connect

from agent_backend.config import RuntimeConfig
from agent_backend.wire import Peer
from bucket_runtime.runtime import Runtime
from bucket_runtime.sandbox_executor import SandboxExecutor
from bucket_runtime.supervisor import Supervisor

log = logging.getLogger(__name__)


def rpc_app(runtime: Runtime, token: str) -> FastAPI:
    app = FastAPI(title="Private runtime RPC", docs_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        from fastapi.responses import JSONResponse
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.post("/plugin/ready")
    async def plugin_ready(request: Request):
        if (await request.json()).get("version") != "3":
            raise HTTPException(409, "plugin protocol mismatch")
        runtime.plugin_ready = True
        return {"ok": True}

    @app.post("/plugin/event")
    async def plugin_event(request: Request):
        try:
            return runtime.plugin_event(await request.json())
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/authorize")
    async def authorize(request: Request):
        body = await request.json()
        try:
            if not isinstance(body.get("directory"), str) or not body["directory"]:
                raise ValueError("plugin must supply its session directory")
            await runtime.bind_tool_session(body["session_id"], body["directory"])
            return runtime.authorize(body["session_id"], body["call_id"], body["tool"], body["args"])
        except (PermissionError, ValueError) as exc:
            raise HTTPException(403, str(exc)) from exc

    @app.post("/execute")
    async def execute(request: Request):
        try:
            return await runtime.execute((await request.json())["ticket"])
        except (PermissionError, ValueError, RuntimeError, TimeoutError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/publish")
    async def publish(request: Request):
        try:
            return await runtime.publish((await request.json())["ticket"])
        except (PermissionError, ValueError, RuntimeError, TimeoutError) as exc:
            raise HTTPException(400, str(exc)) from exc

    return app


def health_app(runtime: Runtime) -> FastAPI:
    app = FastAPI(docs_url=None, openapi_url=None)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        if (not runtime.ready or runtime.draining or not runtime.plugin_ready or not runtime.peer
                or runtime.peer.closed):
            raise HTTPException(503, "runtime not ready")
        return {"status": "ready", "active": len(runtime.tasks)}

    return app


async def serve(config: RuntimeConfig) -> None:
    supervisor = Supervisor(config)
    executor = SandboxExecutor(config.sandbox, config.limits.output_bytes)
    runtime = Runtime(config, supervisor.client, executor)
    rpc_server = uvicorn.Server(uvicorn.Config(rpc_app(runtime, supervisor.rpc_token),
                                               host="127.0.0.1", port=config.rpc_port, log_level="warning"))
    health_server = uvicorn.Server(uvicorn.Config(health_app(runtime), host="0.0.0.0",
                                                port=config.health_port, log_level="warning"))
    tasks = [asyncio.create_task(rpc_server.serve()), asyncio.create_task(health_server.serve())]

    async def connection_loop():
        while not runtime.draining:
            try:
                async with connect(config.control_url, additional_headers={
                    "X-Bucket-ID": config.bucket_id}, max_size=1024 * 1024,
                    ping_interval=20, ping_timeout=20, proxy=None) as socket:
                    peer = Peer(socket, runtime.db, config.bucket_id, runtime.on_command, runtime.rpc)
                    runtime.peer = peer
                    await peer.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("control connection lost: %s", type(exc).__name__)
                await asyncio.sleep(1)
            finally:
                runtime.peer = None

    try:
        for _ in range(100):
            if rpc_server.started and health_server.started:
                break
            if any(t.done() for t in tasks):
                raise RuntimeError("runtime listener failed")
            await asyncio.sleep(0.05)
        if (runtime.permissions.defaults.sandbox.enabled or any(
                runtime.permissions.for_user(u.frontend_id, u.user_id).sandbox.enabled
                for u in runtime.permissions.users)):
            await executor.probe(config.state_root / "sandbox-probe")
        await supervisor.start()
        # Force trusted instance initialization and therefore plugin loading before readiness.
        response = await supervisor.client.http.get("/config")
        response.raise_for_status()
        response = await supervisor.client.http.get("/agent")
        response.raise_for_status()
        for _ in range(100):
            if runtime.plugin_ready:
                break
            await asyncio.sleep(0.1)
        if not runtime.plugin_ready:
            raise RuntimeError("runtime plugin handshake missing")
        await runtime.recover()
        runtime.ready = True
        tasks.extend([asyncio.create_task(connection_loop()),
                      asyncio.create_task(supervisor.process.wait())])
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        runtime.ready = False
        await runtime.shutdown()
        rpc_server.should_exit = health_server.should_exit = True
        await supervisor.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        runtime.db.close()
