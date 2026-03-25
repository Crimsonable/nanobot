from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from urllib import error, request


REQUEST_ID_RE = re.compile(r"request_id=([A-Za-z0-9_:-]+)")
TIMESTAMP_RE = re.compile(r"^(\S+)\s+(.*)$")


def utc_now_rfc3339() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def http_json(method: str, url: str, payload: dict[str, object] | None = None) -> tuple[int, str]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    opener = request.build_opener(request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=180) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body


def docker_logs(container: str, since: str) -> list[tuple[datetime, str]]:
    proc = subprocess.run(
        ["docker", "logs", "--timestamps", "--since", since, container],
        check=False,
        capture_output=True,
        text=True,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    lines: list[tuple[datetime, str]] = []
    for raw in output.splitlines():
        match = TIMESTAMP_RE.match(raw)
        if not match:
            continue
        ts_text, message = match.groups()
        try:
            ts = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
        except ValueError:
            continue
        lines.append((ts, message))
    return lines


def find_request_id(container_logs: list[tuple[datetime, str]], conversation_id: str) -> str:
    for _, message in container_logs:
        if (
            "bridge submit start" in message
            and f"conversation_id={conversation_id}" in message
        ):
            match = REQUEST_ID_RE.search(message)
            if match:
                return match.group(1)
    raise RuntimeError(f"request_id not found for conversation_id={conversation_id}")


def find_event_time(
    lines: list[tuple[datetime, str]],
    *,
    contains: str,
    request_id: str,
) -> datetime:
    for ts, message in lines:
        if contains in message and f"request_id={request_id}" in message:
            return ts
    raise RuntimeError(f"log event not found: contains={contains!r} request_id={request_id}")


def get_child_container_name(base_url: str, org_id: str) -> str:
    status, body = http_json("GET", f"{base_url}/api/org/{org_id}")
    if status != 200:
        raise RuntimeError(f"failed to query /api/org/{org_id}: {status} {body}")
    payload = json.loads(body)
    return str(payload["record"]["container_name"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--sender-uid", default="user-1")
    parser.add_argument("--content", default="Reply with exactly: OK")
    args = parser.parse_args()

    since = utc_now_rfc3339()
    conversation_id = f"debug-latency-{int(time.time() * 1000)}"
    payload = {
        "sender_uid": args.sender_uid,
        "chat_id": conversation_id,
        "content": args.content,
    }

    status, body = http_json("POST", f"{args.base_url}/api/debug/p2p", payload)
    print(f"debug endpoint status: {status}")
    print(f"debug endpoint body: {body}")

    time.sleep(2)

    container_logs = docker_logs("container-up", since)
    request_id = find_request_id(container_logs, conversation_id)
    child_container = get_child_container_name(args.base_url, args.sender_uid)
    child_logs = docker_logs(child_container, since)

    child_final_ready = find_event_time(
        child_logs,
        contains="local_service final ready",
        request_id=request_id,
    )
    child_send_parent = find_event_time(
        child_logs,
        contains="org_router sent_parent",
        request_id=request_id,
    )
    parent_received = find_event_time(
        container_logs,
        contains="bridge child packet",
        request_id=request_id,
    )

    model_to_child_send_ms = (child_send_parent - child_final_ready).total_seconds() * 1000
    child_send_to_parent_receive_ms = (parent_received - child_send_parent).total_seconds() * 1000
    model_to_parent_receive_ms = (parent_received - child_final_ready).total_seconds() * 1000

    print(f"conversation_id: {conversation_id}")
    print(f"request_id: {request_id}")
    print(f"child_container: {child_container}")
    print(f"child_final_ready: {child_final_ready.isoformat()}")
    print(f"child_sent_parent: {child_send_parent.isoformat()}")
    print(f"container_up_received: {parent_received.isoformat()}")
    print(f"model_to_child_send_ms: {model_to_child_send_ms:.3f}")
    print(f"child_send_to_parent_receive_ms: {child_send_to_parent_receive_ms:.3f}")
    print(f"model_to_parent_receive_ms: {model_to_parent_receive_ms:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
