"""Minimal container_up service for dynamic nanobot bridge containers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import docker
import uvicorn
from container_up.bridge_hub import BridgeHub
from docker.errors import APIError, DockerException, NotFound
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from pydantic import AliasChoices, BaseModel, Field

APP_HOST = os.getenv("CONTAINER_UP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("CONTAINER_UP_PORT", "8080"))
DB_PATH = Path(os.getenv("CONTAINER_UP_DB_PATH", "/var/lib/container_up/container_up.db"))
HOST_WORKSPACE_ROOT = Path(os.getenv("HOST_WORKSPACE_ROOT", "/opt/nanobot/workspaces"))
SOURCE_TEMPLATE_CONFIG = Path(
    os.getenv("SOURCE_TEMPLATE_CONFIG", "/opt/nanobot/source_template/config.json")
)
ROOT_TEMPLATE_CONFIG = HOST_WORKSPACE_ROOT / "config.json"
CHILD_IMAGE = os.getenv("CHILD_IMAGE", "nanobot-bridge:latest")
CHILD_NETWORK = os.getenv("CHILD_NETWORK", "nanobot-stack")
CHILD_WORKSPACE_TARGET = os.getenv("CHILD_WORKSPACE_TARGET", "/app/workspace")
CHILD_GATEWAY_PORT = int(os.getenv("CHILD_GATEWAY_PORT", "18790"))
CHILD_BRIDGE_TOKEN = os.getenv("CHILD_BRIDGE_TOKEN", "")
CHILD_READY_TIMEOUT = int(os.getenv("CHILD_READY_TIMEOUT", "90"))
FORWARD_TIMEOUT = float(os.getenv("FORWARD_TIMEOUT", "300"))
CONTAINER_PREFIX = os.getenv("CHILD_CONTAINER_PREFIX", "nanobot-session")
MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "vllm")
MODEL_API_KEY = os.getenv("MODEL_API_KEY", "")
MODEL_API_BASE = os.getenv("MODEL_API_BASE", "")
MODEL_NAME = os.getenv("MODEL_NAME", "")
HOST_SKILLS_SOURCE = os.getenv("HOST_SKILLS_SOURCE", "")
CHILD_SKILLS_DIR = os.getenv("CHILD_SKILLS_DIR", "")
PARENT_BRIDGE_URL = os.getenv("PARENT_BRIDGE_URL", f"ws://container-up:{APP_PORT}/ws/bridge")
IDLE_TIMEOUT_SECONDS = int(os.getenv("IDLE_TIMEOUT_SECONDS", "3600"))
CLEANUP_SCAN_INTERVAL = int(os.getenv("CLEANUP_SCAN_INTERVAL", "300"))

docker_client = docker.from_env()
db_lock = threading.Lock()
session_locks: dict[str, threading.Lock] = {}
session_locks_guard = threading.Lock()
cleanup_stop_event = threading.Event()
cleanup_thread: threading.Thread | None = None


class MessageRequest(BaseModel):
    session_id: str
    user_id: str = Field(validation_alias=AliasChoices("user_id", "usr_id"))
    content: str
    tenant_id: str = "default"
    request_id: str | None = None
    attachments: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = 300.0


class CancelRequest(BaseModel):
    session_id: str
    user_id: str = Field(
        default="remote-control",
        validation_alias=AliasChoices("user_id", "usr_id"),
    )
    tenant_id: str = "default"
    request_id: str = ""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_routes (
                session_id TEXT PRIMARY KEY,
                container_name TEXT NOT NULL,
                container_id TEXT NOT NULL,
                bridge_url TEXT NOT NULL,
                bridge_token TEXT NOT NULL,
                workspace_path TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL
            )
            """
        )
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(session_routes)").fetchall()
        }
        if "last_active_at" not in columns:
            conn.execute(
                f"ALTER TABLE session_routes ADD COLUMN last_active_at TEXT NOT NULL DEFAULT '{now}'"
            )
        conn.commit()


@contextmanager
def db_conn() -> Any:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def get_session_lock(session_id: str) -> threading.Lock:
    with session_locks_guard:
        lock = session_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            session_locks[session_id] = lock
        return lock


def list_session_records() -> list[dict[str, Any]]:
    with db_lock, db_conn() as conn:
        rows = conn.execute("SELECT * FROM session_routes ORDER BY session_id").fetchall()
    return [dict(row) for row in rows]


def session_record(session_id: str) -> dict[str, Any] | None:
    with db_lock, db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM session_routes WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def upsert_session_record(record: dict[str, Any]) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    last_active_at = record.get("last_active_at", now)
    with db_lock, db_conn() as conn:
        conn.execute(
            """
            INSERT INTO session_routes (
                session_id, container_name, container_id, bridge_url, bridge_token,
                workspace_path, status, created_at, updated_at, last_active_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                container_name=excluded.container_name,
                container_id=excluded.container_id,
                bridge_url=excluded.bridge_url,
                bridge_token=excluded.bridge_token,
                workspace_path=excluded.workspace_path,
                status=excluded.status,
                updated_at=excluded.updated_at,
                last_active_at=excluded.last_active_at
            """,
            (
                record["session_id"],
                record["container_name"],
                record["container_id"],
                record["bridge_url"],
                record["bridge_token"],
                record["workspace_path"],
                record["status"],
                record.get("created_at", now),
                now,
                last_active_at,
            ),
        )
        conn.commit()


def touch_session(session_id: str) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with db_lock, db_conn() as conn:
        conn.execute(
            "UPDATE session_routes SET last_active_at = ?, updated_at = ? WHERE session_id = ?",
            (now, now, session_id),
        )
        conn.commit()


def delete_session_record(session_id: str) -> None:
    with db_lock, db_conn() as conn:
        conn.execute("DELETE FROM session_routes WHERE session_id = ?", (session_id,))
        conn.commit()


def safe_name(session_id: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_.-]+", "-", session_id).strip("-.") or "session"
    short_hash = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:8]
    return f"{CONTAINER_PREFIX}-{base[:40]}-{short_hash}"


def session_workspace_path(session_id: str) -> Path:
    return HOST_WORKSPACE_ROOT / session_id


def apply_managed_config(config: dict[str, Any]) -> tuple[dict[str, Any], str]:
    channels = config.setdefault("channels", {})
    bridge = channels.setdefault("bridge", {})
    bridge["enabled"] = True
    bridge["bridgeUrl"] = PARENT_BRIDGE_URL

    token = CHILD_BRIDGE_TOKEN or str(bridge.get("bridgeToken") or "")
    bridge["bridgeToken"] = token

    providers = config.setdefault("providers", {})
    provider_cfg = providers.setdefault(MODEL_PROVIDER, {})
    if MODEL_API_KEY:
        provider_cfg["apiKey"] = MODEL_API_KEY
    if MODEL_API_BASE:
        provider_cfg["apiBase"] = MODEL_API_BASE

    if MODEL_NAME:
        agents = config.setdefault("agents", {})
        defaults = agents.setdefault("defaults", {})
        defaults["model"] = MODEL_NAME

    return config, token


def ensure_root_template_config() -> tuple[dict[str, Any], str]:
    HOST_WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    if not ROOT_TEMPLATE_CONFIG.exists():
        if not SOURCE_TEMPLATE_CONFIG.exists():
            raise RuntimeError(
                f"root config missing at {ROOT_TEMPLATE_CONFIG} and source template missing at {SOURCE_TEMPLATE_CONFIG}"
            )
        shutil.copyfile(SOURCE_TEMPLATE_CONFIG, ROOT_TEMPLATE_CONFIG)

    config = load_json(ROOT_TEMPLATE_CONFIG)
    updated, token = apply_managed_config(config)
    save_json(ROOT_TEMPLATE_CONFIG, updated)
    return updated, token


def ensure_session_workspace(session_id: str) -> tuple[Path, str, bool]:
    _, token = ensure_root_template_config()
    workspace_path = session_workspace_path(session_id)
    config_path = workspace_path / "config.json"
    created = False

    if not workspace_path.exists():
        workspace_path.mkdir(parents=True, exist_ok=True)
        created = True

    if not config_path.exists():
        shutil.copyfile(ROOT_TEMPLATE_CONFIG, config_path)
        created = True

    config = load_json(config_path)
    before = json.dumps(config, sort_keys=True, ensure_ascii=False)
    updated, token = apply_managed_config(config)
    after = json.dumps(updated, sort_keys=True, ensure_ascii=False)
    changed = before != after or created
    if changed:
        save_json(config_path, updated)
    return workspace_path, token, changed


def get_container(name: str) -> Any | None:
    try:
        return docker_client.containers.get(name)
    except NotFound:
        return None


def container_running(container: Any) -> bool:
    try:
        container.reload()
    except NotFound:
        return False
    return container.status == "running"


def bridge_connected(session_id: str) -> bool:
    return get_bridge_hub().child_for_session(session_id) is not None


def wait_until_ready(session_id: str) -> None:
    deadline = time.time() + CHILD_READY_TIMEOUT
    while time.time() < deadline:
        if bridge_connected(session_id):
            return
        time.sleep(1)
    raise RuntimeError(f"bridge channel did not register before timeout: {session_id}")


def remove_container(container: Any) -> None:
    try:
        container.remove(force=True)
    except NotFound:
        return


def restart_container(container: Any, session_id: str) -> None:
    container.restart(timeout=20)
    wait_until_ready(session_id)


def build_child_volumes(workspace_path: Path) -> dict[str, dict[str, str]]:
    volumes: dict[str, dict[str, str]] = {
        str(workspace_path): {
            "bind": CHILD_WORKSPACE_TARGET,
            "mode": "rw",
        }
    }
    if HOST_SKILLS_SOURCE and CHILD_SKILLS_DIR:
        volumes[HOST_SKILLS_SOURCE] = {
            "bind": CHILD_SKILLS_DIR,
            "mode": "ro",
        }
    return volumes


def ensure_child_network() -> None:
    if not CHILD_NETWORK:
        raise RuntimeError("CHILD_NETWORK must be configured")
    try:
        docker_client.networks.get(CHILD_NETWORK)
    except NotFound:
        docker_client.networks.create(CHILD_NETWORK, driver="bridge")


def create_child_container(session_id: str, container_name: str, workspace_path: Path, token: str) -> dict[str, Any]:
    environment = {
        "BRIDGE_SESSION_ID": session_id,
        "BRIDGE_CONTAINER_NAME": container_name,
        "GATEWAY_PORT": str(CHILD_GATEWAY_PORT),
    }
    if token:
        environment["BRIDGE_TOKEN_OVERRIDE"] = token

    try:
        container = docker_client.containers.run(
            CHILD_IMAGE,
            name=container_name,
            detach=True,
            network=CHILD_NETWORK,
            restart_policy={"Name": "unless-stopped"},
            labels={"managed-by": "container_up", "session-id": session_id},
            environment=environment,
            volumes=build_child_volumes(workspace_path),
        )
    except APIError as exc:
        raise RuntimeError(f"failed to create child container: {exc.explanation}") from exc

    bridge_url = PARENT_BRIDGE_URL
    wait_until_ready(session_id)
    record = {
        "session_id": session_id,
        "container_name": container_name,
        "container_id": container.id,
        "bridge_url": bridge_url,
        "bridge_token": token,
        "workspace_path": str(workspace_path),
        "status": "running",
        "last_active_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    upsert_session_record(record)
    return record


def parse_ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return 0.0


def cleanup_idle_sessions() -> None:
    if IDLE_TIMEOUT_SECONDS <= 0:
        return
    cutoff = time.time() - IDLE_TIMEOUT_SECONDS
    for record in list_session_records():
        if parse_ts(record.get("last_active_at")) >= cutoff:
            continue
        session_id = record["session_id"]
        with get_session_lock(session_id):
            current = session_record(session_id)
            if current is None:
                continue
            if parse_ts(current.get("last_active_at")) >= cutoff:
                continue
            container = get_container(current["container_name"])
            if container is not None:
                remove_container(container)
            delete_session_record(session_id)


def cleanup_loop() -> None:
    while not cleanup_stop_event.wait(CLEANUP_SCAN_INTERVAL):
        try:
            cleanup_idle_sessions()
        except Exception:
            continue


def sync_existing_sessions() -> None:
    ensure_root_template_config()
    for record in list_session_records():
        session_id = record["session_id"]
        workspace_path, token, changed = ensure_session_workspace(session_id)
        bridge_url = PARENT_BRIDGE_URL
        updated_record = {
            **record,
            "bridge_url": bridge_url,
            "bridge_token": token,
            "workspace_path": str(workspace_path),
            "status": record.get("status", "running"),
            "last_active_at": record.get("last_active_at"),
        }
        upsert_session_record(updated_record)

        container = get_container(record["container_name"])
        if container and container_running(container):
            if changed:
                container.restart(timeout=20)


def ensure_session_container(session_id: str) -> dict[str, Any]:
    with get_session_lock(session_id):
        record = session_record(session_id)
        if record:
            workspace_path, token, changed = ensure_session_workspace(session_id)
            bridge_url = PARENT_BRIDGE_URL
            container = get_container(record["container_name"])
            if container and container_running(container):
                try:
                    if changed:
                        restart_container(container, session_id)
                    else:
                        wait_until_ready(session_id)
                    updated = {
                        **record,
                        "bridge_url": bridge_url,
                        "bridge_token": token,
                        "workspace_path": str(workspace_path),
                        "status": "running",
                        "last_active_at": record.get("last_active_at"),
                    }
                    upsert_session_record(updated)
                    return updated
                except RuntimeError:
                    pass
            if container is not None:
                remove_container(container)
            delete_session_record(session_id)

        workspace_path, token, _ = ensure_session_workspace(session_id)
        ensure_child_network()
        container_name = safe_name(session_id)
        existing = get_container(container_name)
        if existing is not None:
            if container_running(existing):
                bridge_url = PARENT_BRIDGE_URL
                wait_until_ready(session_id)
                record = {
                    "session_id": session_id,
                    "container_name": container_name,
                    "container_id": existing.id,
                    "bridge_url": bridge_url,
                    "bridge_token": token,
                    "workspace_path": str(workspace_path),
                    "status": "running",
                    "last_active_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                upsert_session_record(record)
                return record
            remove_container(existing)
        return create_child_container(session_id, container_name, workspace_path, token)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    ensure_child_network()
    docker_client.ping()
    sync_existing_sessions()
    cleanup_stop_event.clear()
    global cleanup_thread
    cleanup_thread = threading.Thread(target=cleanup_loop, name="container-up-cleanup", daemon=True)
    cleanup_thread.start()
    yield
    cleanup_stop_event.set()
    if cleanup_thread is not None:
        cleanup_thread.join(timeout=5)


app = FastAPI(title="container_up", version="0.1.0", lifespan=lifespan)
app.state.bridge_hub = BridgeHub(token=CHILD_BRIDGE_TOKEN or None)


def get_bridge_hub() -> BridgeHub:
    return app.state.bridge_hub


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    try:
        docker_client.ping()
        docker_ok = True
    except DockerException:
        docker_ok = False
    with db_lock, db_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM session_routes").fetchone()[0]
    bridge_hub = get_bridge_hub()
    return {
        "status": "ok" if docker_ok else "degraded",
        "docker": docker_ok,
        "tracked_sessions": count,
        "connected_bridge_sessions": bridge_hub.child_count,
    }


@app.websocket("/ws/bridge")
async def bridge_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    bridge_hub = get_bridge_hub()
    session_id: str | None = None
    try:
        raw = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        session_id = await bridge_hub.register_child(websocket, raw)
        if session_id is None:
            return

        while True:
            packet = await websocket.receive_json()
            await bridge_hub.handle_child_packet(session_id, packet)
    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        await websocket.close(code=4001, reason="register timeout")
    finally:
        if session_id is not None:
            bridge_hub.unregister_child(session_id, websocket)


@app.get("/api/session/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    record = session_record(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="session not found")
    container = get_container(record["container_name"])
    child = get_bridge_hub().child_for_session(session_id)
    return {
        "session_id": session_id,
        "record": record,
        "container_status": getattr(container, "status", "missing") if container else "missing",
        "bridge_connected": child is not None,
    }


@app.post("/api/message")
async def post_message(payload: MessageRequest) -> dict[str, Any]:
    try:
        await run_in_threadpool(ensure_session_container, payload.session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await run_in_threadpool(touch_session, payload.session_id)

    try:
        return await get_bridge_hub().submit_message(
            session_id=payload.session_id,
            conversation_id=payload.session_id,
            user_id=payload.user_id,
            tenant_id=payload.tenant_id,
            content=payload.content,
            request_id=payload.request_id,
            attachments=payload.attachments,
            metadata=payload.metadata,
            timeout=payload.timeout_seconds,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="bridge response timeout") from exc


@app.post("/api/cancel")
async def post_cancel(payload: CancelRequest) -> dict[str, Any]:
    record = await run_in_threadpool(session_record, payload.session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="session not found")
    await run_in_threadpool(touch_session, payload.session_id)
    try:
        return await get_bridge_hub().submit_cancel(
            session_id=payload.session_id,
            conversation_id=payload.session_id,
            user_id=payload.user_id,
            tenant_id=payload.tenant_id,
            request_id=payload.request_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def main() -> None:
    uvicorn.run("container_up.app:app", host=APP_HOST, port=APP_PORT)


if __name__ == "__main__":
    main()
