from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import docker
from container_up.bridge_state import bridge_connected
from container_up.db_store import (
    delete_org_record,
    list_org_records,
    org_record,
    upsert_org_record,
)
from container_up.frontend_config import FrontendConfig, frontend_config_for
from container_up.settings import (
    CHILD_BRIDGE_TOKEN,
    CHILD_CONTAINER_PREFIX,
    CHILD_IMAGE,
    CHILD_NETWORK,
    CHILD_NANOBOT_SOURCE_TARGET,
    CHILD_READY_TIMEOUT,
    CHILD_SHARED_CONFIG_TARGET,
    CHILD_WORKSPACE_TARGET,
    HOST_NANOBOT_SOURCE,
    HOST_SHARED_CONFIG,
    HOST_WORKSPACE_ROOT,
    IDLE_TIMEOUT_SECONDS,
    INSTANCE_IDLE_TIMEOUT_SECONDS,
    LOG_LLM_REQUESTS,
    PARENT_BRIDGE_URL,
)
from docker.errors import APIError, NotFound

docker_client = docker.from_env()
org_locks: dict[str, threading.Lock] = {}
org_locks_guard = threading.Lock()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_org_lock(org_id: str) -> threading.Lock:
    with org_locks_guard:
        lock = org_locks.get(org_id)
        if lock is None:
            lock = threading.Lock()
            org_locks[org_id] = lock
        return lock


def safe_name(org_id: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_.-]+", "-", org_id).strip("-.") or "org"
    short_hash = hashlib.sha1(org_id.encode("utf-8")).hexdigest()[:8]
    return f"{CHILD_CONTAINER_PREFIX}-{base[:40]}-{short_hash}"


def org_workspace_path(org_id: str) -> Path:
    return HOST_WORKSPACE_ROOT / org_id


def load_shared_config() -> tuple[dict[str, Any], str]:
    HOST_WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    if not HOST_SHARED_CONFIG.exists():
        raise RuntimeError(f"shared config template missing at {HOST_SHARED_CONFIG}")
    config = load_json(HOST_SHARED_CONFIG)
    bridge = config.get("channels", {}).get("bridge", {})
    token = CHILD_BRIDGE_TOKEN or str(bridge.get("bridgeToken") or "")
    return config, token


def ensure_org_workspace(org_id: str) -> tuple[Path, str, bool]:
    _, token = load_shared_config()
    workspace_path = org_workspace_path(org_id)
    created = not workspace_path.exists()
    workspace_path.mkdir(parents=True, exist_ok=True)
    return workspace_path, token, created


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


def wait_until_ready(org_id: str) -> None:
    deadline = time.time() + CHILD_READY_TIMEOUT
    while time.time() < deadline:
        if bridge_connected(org_id):
            return
        time.sleep(1)
    raise RuntimeError(f"bridge channel did not register before timeout: {org_id}")


def remove_container(container: Any) -> None:
    try:
        container.remove(force=True)
    except NotFound:
        return


def restart_container(container: Any, org_id: str) -> None:
    container.restart(timeout=20)
    wait_until_ready(org_id)


def build_child_volumes(
    workspace_path: Path,
    frontend_config: FrontendConfig | None = None,
) -> dict[str, dict[str, str]]:
    volumes: dict[str, dict[str, str]] = {
        str(workspace_path): {
            "bind": CHILD_WORKSPACE_TARGET,
            "mode": "rw",
        },
        str(HOST_SHARED_CONFIG): {
            "bind": CHILD_SHARED_CONFIG_TARGET,
            "mode": "ro",
        },
    }
    if HOST_NANOBOT_SOURCE:
        if not HOST_NANOBOT_SOURCE.exists():
            raise RuntimeError(f"nanobot source missing at {HOST_NANOBOT_SOURCE}")
        volumes[str(HOST_NANOBOT_SOURCE)] = {
            "bind": CHILD_NANOBOT_SOURCE_TARGET,
            "mode": "ro",
        }
    if frontend_config is not None:
        if frontend_config.builtin_skills_dir is not None:
            volumes[str(frontend_config.builtin_skills_dir)] = {
                "bind": frontend_config.child_builtin_skills_dir,
                "mode": "ro",
            }
        if frontend_config.template_dir is not None:
            volumes[str(frontend_config.template_dir)] = {
                "bind": frontend_config.child_template_dir,
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


def create_child_container(
    org_id: str,
    container_name: str,
    workspace_path: Path,
    token: str,
    frontend_config: FrontendConfig | None = None,
) -> dict[str, Any]:
    environment = {
        "BRIDGE_ORG_ID": org_id,
        "BRIDGE_CONTAINER_NAME": container_name,
        "PARENT_BRIDGE_URL": PARENT_BRIDGE_URL,
        "INSTANCE_IDLE_TIMEOUT_SECONDS": str(INSTANCE_IDLE_TIMEOUT_SECONDS),
    }
    if LOG_LLM_REQUESTS:
        environment["NANOBOT_LOG_LLM_REQUESTS"] = LOG_LLM_REQUESTS
    if token:
        environment["BRIDGE_TOKEN_OVERRIDE"] = token
    if frontend_config is not None:
        if frontend_config.builtin_skills_dir is not None:
            environment["BUILTIN_SKILLS_DIR"] = frontend_config.child_builtin_skills_dir
        if frontend_config.template_dir is not None:
            environment["TEMPLATE_DIR"] = frontend_config.child_template_dir

    run_kwargs: dict[str, Any] = {
        "name": container_name,
        "detach": True,
        "restart_policy": {"Name": "unless-stopped"},
        "labels": {"managed-by": "container_up", "org-id": org_id},
        "environment": environment,
        "volumes": build_child_volumes(workspace_path, frontend_config),
        "network": CHILD_NETWORK,
    }

    try:
        container = docker_client.containers.run(
            CHILD_IMAGE,
            **run_kwargs,
        )
    except APIError as exc:
        raise RuntimeError(
            f"failed to create child container: {exc.explanation}"
        ) from exc

    record = {
        "org_id": org_id,
        "frontend_id": frontend_config.id if frontend_config is not None else "",
        "container_name": container_name,
        "container_id": container.id,
        "bridge_url": PARENT_BRIDGE_URL,
        "bridge_token": token,
        "workspace_path": str(workspace_path),
        "status": "running",
        "last_active_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    wait_until_ready(org_id)
    upsert_org_record(record)
    return record


def parse_ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return 0.0


def cleanup_idle_orgs() -> None:
    if IDLE_TIMEOUT_SECONDS <= 0:
        return
    cutoff = time.time() - IDLE_TIMEOUT_SECONDS
    for record in list_org_records():
        if parse_ts(record.get("last_active_at")) >= cutoff:
            continue
        org_id = record["org_id"]
        with get_org_lock(org_id):
            current = org_record(org_id)
            if current is None:
                continue
            if parse_ts(current.get("last_active_at")) >= cutoff:
                continue
            container = get_container(current["container_name"])
            if container is not None:
                remove_container(container)
            delete_org_record(org_id)


def shutdown_org_container(org_id: str) -> dict[str, Any]:
    with get_org_lock(org_id):
        record = org_record(org_id)
        container_name = safe_name(org_id)
        container = get_container(record["container_name"]) if record else None
        if container is None:
            container = get_container(container_name)

        removed = False
        if container is not None:
            remove_container(container)
            removed = True

        if record is not None:
            delete_org_record(org_id)

        return {
            "org_id": org_id,
            "container_name": getattr(container, "name", container_name),
            "removed": removed,
            "record_deleted": record is not None,
        }


def shutdown_all_org_containers() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    org_ids = [str(record["org_id"]) for record in list_org_records()]
    for org_id in org_ids:
        results.append(shutdown_org_container(org_id))
    return results


def sync_existing_orgs() -> None:
    load_shared_config()
    for record in list_org_records():
        org_id = record["org_id"]
        workspace_path, token, changed = ensure_org_workspace(org_id)
        updated_record = {
            **record,
            "bridge_url": PARENT_BRIDGE_URL,
            "bridge_token": token,
            "workspace_path": str(workspace_path),
            "status": record.get("status", "running"),
            "last_active_at": record.get("last_active_at"),
        }
        upsert_org_record(updated_record)

        container = get_container(record["container_name"])
        if container and container_running(container) and changed:
            container.restart(timeout=20)


def ensure_org_container(org_id: str, frontend_id: str | None = None) -> dict[str, Any]:
    with get_org_lock(org_id):
        frontend_config = frontend_config_for(frontend_id)
        record = org_record(org_id)
        if record:
            workspace_path, token, changed = ensure_org_workspace(org_id)
            container = get_container(record["container_name"])
            expected_frontend_id = frontend_config.id if frontend_config is not None else ""
            frontend_changed = str(record.get("frontend_id") or "") != expected_frontend_id
            if container and container_running(container):
                try:
                    if frontend_changed:
                        raise RuntimeError("frontend configuration changed")
                    if changed:
                        restart_container(container, org_id)
                    else:
                        wait_until_ready(org_id)
                    updated = {
                        **record,
                        "frontend_id": expected_frontend_id,
                        "bridge_url": PARENT_BRIDGE_URL,
                        "bridge_token": token,
                        "workspace_path": str(workspace_path),
                        "status": "running",
                        "last_active_at": record.get("last_active_at"),
                    }
                    upsert_org_record(updated)
                    return updated
                except RuntimeError:
                    pass
            if container is not None:
                remove_container(container)
            delete_org_record(org_id)

        workspace_path, token, _ = ensure_org_workspace(org_id)
        ensure_child_network()
        container_name = safe_name(org_id)
        existing = get_container(container_name)
        if existing is not None:
            if container_running(existing):
                wait_until_ready(org_id)
                record = {
                    "org_id": org_id,
                    "frontend_id": frontend_config.id if frontend_config is not None else "",
                    "container_name": container_name,
                    "container_id": existing.id,
                    "bridge_url": PARENT_BRIDGE_URL,
                    "bridge_token": token,
                    "workspace_path": str(workspace_path),
                    "status": "running",
                    "last_active_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    ),
                }
                upsert_org_record(record)
                return record
            remove_container(existing)
        return create_child_container(
            org_id,
            container_name,
            workspace_path,
            token,
            frontend_config,
        )
