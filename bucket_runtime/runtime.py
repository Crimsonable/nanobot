from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import secrets
import shutil
import time
from pathlib import Path

from agent_backend.config import RuntimeConfig
from agent_backend.journal import Journal
from agent_backend.models import TERMINAL, Attachment, Envelope, Result, Submit, digest, uid
from agent_backend.transfers import Transfers, file_hash, send_file
from agent_backend.wire import Peer
from bucket_runtime.opencode_client import OpenCodeClient, final_message, usage_for
from bucket_runtime.sandbox_executor import SandboxExecutor, contained

log = logging.getLogger(__name__)


def session_permissions() -> list[dict]:
    # Native filesystem tools cannot provide race-free host access through a before hook.
    # Keep the verified execution surface small: bash is always wrapped; publishing is a custom tool.
    return [{"permission": "*", "pattern": "*", "action": "deny"},
            {"permission": "bash", "pattern": "*", "action": "allow"},
            {"permission": "publish_attachment", "pattern": "*", "action": "allow"}]


class Runtime:
    def __init__(self, config: RuntimeConfig, client: OpenCodeClient, executor: SandboxExecutor):
        self.config, self.client, self.executor = config, client, executor
        self.db = Journal(config.state_root / "runtime.sqlite", config.limits.outbox_messages)
        self.transfers = Transfers(config.state_root / "incoming", self.db,
                                   config.limits.file_bytes, config.limits.spool_bytes)
        self.peer: Peer | None = None
        self.tasks: dict[str, asyncio.Task] = {}
        self.request_locks: dict[str, asyncio.Lock] = {}
        self.file_locks: dict[str, asyncio.Lock] = {}
        self.capacity = asyncio.Semaphore(config.limits.active)
        self.draining = False
        self.ready = False
        self.plugin_ready = False
        self.last_text_event: dict[str, float] = {}

    def workspace(self, submit: Submit) -> Path:
        path = self.config.workspace_root / submit.identity.key
        if path.is_symlink():
            raise PermissionError("workspace root is a symlink")
        path.mkdir(parents=True, exist_ok=True)
        for folder in ["inbox", "output", "scratch"]:
            target = path / folder
            if target.is_symlink():
                raise PermissionError("workspace directory is a symlink")
            target.mkdir(exist_ok=True)
        return path.resolve()

    def emit(self, kind: str, request_id: str, payload: dict | None = None) -> None:
        self.db.enqueue(self.config.bucket_id, Envelope(type=kind, bucket_id=self.config.bucket_id,
                        request_id=request_id, payload=payload or {}))

    def finish(self, result: Result) -> None:
        row = self.db.get("run", result.request_id)
        if not row or row["status"] in TERMINAL:
            return
        with self.db.transaction():
            self.db.put("run", result.request_id, {**row, "status": result.status,
                         "result": result.model_dump(mode="json")})
            self.emit("request." + result.status, result.request_id, result.model_dump(mode="json"))
        log.info("request terminal bucket_id=%s request_id=%s status=%s attachments=%d",
                 self.config.bucket_id, result.request_id, result.status, len(result.attachments))
        for intent in self.db.records("intent"):
            if intent["request_id"] == result.request_id:
                (self.config.state_root / "snapshots" / intent["attachment"]["attachment_id"]).unlink(missing_ok=True)
        for transfer in self.db.records("transfer"):
            if transfer["request_id"] == result.request_id and not transfer.get("released"):
                self.transfers.release(self.config.bucket_id, transfer["transfer_id"])

    async def on_command(self, event: Envelope) -> None:
        if event.type == "request.submit":
            submit = Submit.model_validate(event.payload)
            if submit.request_id != event.request_id:
                raise ValueError("request identity mismatch")
            row = self.db.get("run", submit.request_id)
            if row:
                if row["hash"] != digest(event.payload):
                    raise ValueError("request content changed")
                return
            with self.db.transaction():
                self.db.put("run", submit.request_id, {"request_id": submit.request_id,
                    "submit": submit.model_dump(mode="json"), "hash": digest(event.payload),
                    "status": "queued", "artifacts": []})
                self.emit("request.accepted", submit.request_id)
            if (self.draining or submit.profile_id not in self.config.profiles
                    or len(self.tasks) >= self.config.limits.queued + self.config.limits.active):
                self.finish(Result(request_id=submit.request_id, status="failed", error="BUCKET_UNAVAILABLE"))
            else:
                self.launch(submit.request_id)
        elif event.type == "request.cancel":
            row = self.db.get("run", event.request_id)
            if row and row["status"] not in TERMINAL:
                self.db.put("run", event.request_id, {**row, "cancel_requested": True})
                task = self.tasks.get(event.request_id)
                if task:
                    task.cancel()
                else:
                    self.finish(Result(request_id=event.request_id, status="cancelled"))
        elif event.type == "request.query":
            row = self.db.get("run", event.request_id)
            if row and row.get("result"):
                self.emit("request." + row["status"], event.request_id, row["result"])
        else:
            raise ValueError("unsupported control command")

    def launch(self, request_id: str) -> None:
        task = asyncio.create_task(self.run(request_id))
        self.tasks[request_id] = task

        def finished(done: asyncio.Task) -> None:
            self.tasks.pop(request_id, None)
            row = self.db.get("run", request_id)
            if done.cancelled() or done.exception():
                self.finish(Result(request_id=request_id,
                    status="cancelled" if row.get("cancel_requested") else "failed",
                    error=None if row.get("cancel_requested") else "EXECUTION_INTERRUPTED"))

        task.add_done_callback(finished)

    async def recover(self) -> None:
        for row in self.db.records("run"):
            if row["status"] in TERMINAL:
                continue
            if row.get("cancel_requested"):
                self.finish(Result(request_id=row["request_id"], status="cancelled"))
            elif row["status"] == "queued":
                self.launch(row["request_id"])
            else:
                # A new supervisor process cannot prove prior side effects did not happen.
                if row.get("session_id"):
                    await self.client.abort(row["session_id"])
                    messages = await self.client.messages(row["session_id"])
                    final = final_message(messages, row.get("message_id", ""))
                    if final and not final[1]:
                        self.finish(Result(request_id=row["request_id"], status="completed",
                            content=final[0], attachments=row["artifacts"], usage=usage_for(messages, row["message_id"])))
                        continue
                self.finish(Result(request_id=row["request_id"], status="failed",
                    error="EXECUTION_INTERRUPTED", partial=bool(row["artifacts"]), attachments=row["artifacts"]))

    async def run(self, request_id: str) -> None:
        row = self.db.get("run", request_id)
        submit = Submit.model_validate(row["submit"])
        session_id = None
        try:
            async with asyncio.timeout(max(0.01, submit.deadline_at - time.time())):
                async with self.request_locks.setdefault(submit.identity.key, asyncio.Lock()):
                    async with self.capacity:
                        if self.draining or self.db.get("run", request_id).get("cancel_requested"):
                            raise asyncio.CancelledError()
                        workspace = self.workspace(submit)
                        profile = self.config.profiles[submit.profile_id]
                        session = self.db.get("session", submit.conversation_id)
                        if session:
                            if session["user_key"] != submit.identity.key or session["profile"] != submit.profile_id:
                                raise PermissionError("conversation ownership/profile mismatch")
                            session_id = session["session_id"]
                            await self.client.set_permissions(session_id, session_permissions())
                        else:
                            session_id = await self.client.create_session(session_permissions())
                            self.db.put("session", submit.conversation_id, {"session_id": session_id,
                                "user_key": submit.identity.key, "profile": submit.profile_id})
                        row = self.db.get("run", request_id)
                        message_id = uid("msg_")
                        self.db.put("run", request_id, {**row, "status": "preparing",
                                    "session_id": session_id, "message_id": message_id})
                        references = []
                        image_index = 0
                        for attachment in submit.attachments:
                            while True:
                                peer = await self.connected(submit.deadline_at)
                                try:
                                    materialized = await peer.call("input.fetch", {
                                        "request_id": request_id, "attachment_id": attachment.attachment_id})
                                    break
                                except (ConnectionError, TimeoutError):
                                    await asyncio.sleep(0.2)
                            alias = attachment.filename
                            if attachment.content_type.startswith("image/"):
                                image_index += 1
                                alias = f"image_{image_index} ({attachment.filename})"
                            references.append(f"{alias} — {attachment.content_type}: {attachment.url}\n"
                                              f"Local file: {materialized['path']}")
                        text = (profile.instructions + "\nYour writable workspace: " + str(workspace)
                            + "\nRead-only reference/skill directories: "
                            + json.dumps([str(p) for p in profile.public_roots])
                            + "\nUse bash to read files, list directories and run scripts in the sandbox. "
                            + "Use publish_attachment to include generated files in the reply. "
                            + "A published file is queued for delivery; do not claim it was already delivered.\n"
                            + "\n".join(references) + "\nUser message:\n" + submit.content)
                        row = self.db.get("run", request_id)
                        with self.db.transaction():
                            self.db.put("run", request_id, {**row, "status": "running"})
                            self.emit("request.started", request_id)
                        try:
                            await self.client.submit(session_id, message_id, text, profile.model)
                        except Exception:
                            # Submission may have reached OpenCode. Never automatically re-execute it.
                            messages = await self.client.messages(session_id)
                            if not any(m["info"]["id"] == message_id for m in messages):
                                raise RuntimeError("EXECUTION_INTERRUPTED") from None
                        while True:
                            messages = await self.client.messages(session_id)
                            final = final_message(messages, message_id)
                            if final:
                                artifacts = self.db.get("run", request_id)["artifacts"]
                                error = final[1] or (None if final[0] or artifacts else "EMPTY_RESULT")
                                self.finish(Result(request_id=request_id, status="failed" if error else "completed",
                                    content=final[0], attachments=artifacts, error=error,
                                    partial=bool(error and artifacts), usage=usage_for(messages, message_id)))
                                return
                            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            await self.executor.kill(request_id)
            if session_id:
                try:
                    await self.client.abort(session_id)
                except Exception:
                    # Fail closed: no new request may use a possibly still running server.
                    self.draining = True
            row = self.db.get("run", request_id)
            self.finish(Result(request_id=request_id,
                status="cancelled" if row.get("cancel_requested") else "failed",
                error=None if row.get("cancel_requested") else "EXECUTION_INTERRUPTED"))
        except Exception as exc:
            await self.executor.kill(request_id)
            try:
                if session_id:
                    await self.client.abort(session_id)
            except Exception:
                self.draining = True
            finally:
                await self.executor.kill(request_id)
                row = self.db.get("run", request_id)
                self.finish(Result(request_id=request_id, status="failed", error=type(exc).__name__
                    if not isinstance(exc, (RuntimeError, TimeoutError)) else str(exc) or "REQUEST_TIMEOUT",
                    attachments=row["artifacts"], partial=bool(row["artifacts"])))

    async def events(self) -> None:
        async for event in self.client.events():
            properties = event.get("properties") or {}
            session_id = (properties.get("sessionID") or properties.get("part", {}).get("sessionID")
                          or properties.get("info", {}).get("sessionID"))
            if not session_id:
                continue
            row = next((r for r in self.db.records("run") if r.get("session_id") == session_id
                        and r["status"] == "running"), None)
            if row is None:
                continue
            # Raw events never reach a frontend. Only mapped, bounded tool progress is emitted.
            part = properties.get("part") or {}
            if event.get("type") == "message.part.updated" and part.get("type") == "tool":
                self.emit("tool.progress", row["request_id"], {"call_id": part.get("callID"),
                    "tool": part.get("tool"), "status": part.get("state", {}).get("status")})
            is_text = (event.get("type") == "message.part.updated" and part.get("type") == "text"
                       or event.get("type") == "message.part.delta" and properties.get("field") == "text")
            if is_text and time.monotonic() - self.last_text_event.get(row["request_id"], 0) >= 0.5:
                self.last_text_event[row["request_id"]] = time.monotonic()
                messages = await self.client.messages(session_id)
                texts = [p["text"] for m in messages if m["info"].get("parentID") == row["message_id"]
                         and m["info"].get("role") == "assistant" for p in m.get("parts", [])
                         if p.get("type") == "text" and p.get("text") and not p.get("ignored")]
                latest = self.db.get("run", row["request_id"])
                if latest["status"] == "running" and texts:
                    revision = latest.get("text_revision", 0) + 1
                    with self.db.transaction():
                        self.db.put("run", row["request_id"], {**latest, "text_revision": revision})
                        self.emit("response.delta", row["request_id"], {"revision": revision,
                            "content": "\n".join(texts)[-200000:], "mode": "snapshot"})

    def active_session(self, session_id: str) -> tuple[dict, Submit]:
        row = next((r for r in self.db.records("run") if r.get("session_id") == session_id
                    and r["status"] == "running" and not r.get("cancel_requested")), None)
        if not row:
            raise PermissionError("session has no active authorized request")
        return row, Submit.model_validate(row["submit"])

    async def connected(self, deadline: float) -> Peer:
        while not self.peer or self.peer.closed:
            if time.time() >= deadline:
                raise TimeoutError("control connection deadline exceeded")
            await asyncio.sleep(0.2)
        return self.peer

    def authorize(self, session_id: str, call_id: str, tool: str, args: dict) -> dict:
        row, submit = self.active_session(session_id)
        if tool not in {"bash", "publish_attachment"}:
            raise PermissionError("tool is not enabled by runtime policy")
        if tool == "bash" and (not isinstance(args.get("command"), str) or len(args["command"]) > 100000):
            raise ValueError("invalid shell command")
        call_key = digest([session_id, call_id])
        old = self.db.get("call", call_key)
        if old:
            if old["tool"] != tool or old["args"] != args or old["request_id"] != submit.request_id:
                raise PermissionError("tool call identity changed")
            return {"ticket": old["ticket"], "host_workdir": str(self.config.opencode.context_root)}
        ticket = secrets.token_urlsafe(32)
        call = {"ticket": ticket, "request_id": submit.request_id, "session_id": session_id,
                "call_id": call_id, "tool": tool, "args": args, "status": "authorized"}
        with self.db.transaction():
            self.db.put("call", call_key, call)
            self.db.put("ticket", ticket, {"call_key": call_key})
        return {"ticket": ticket, "host_workdir": str(self.config.opencode.context_root)}

    def ticket(self, ticket: str, tool: str) -> tuple[str, dict, Submit]:
        ref = self.db.get("ticket", ticket)
        if not ref:
            raise PermissionError("invalid execution ticket")
        call = self.db.get("call", ref["call_key"])
        if call["tool"] != tool:
            raise PermissionError("ticket tool mismatch")
        row, submit = self.active_session(call["session_id"])
        if row["request_id"] != call["request_id"]:
            raise PermissionError("ticket request mismatch")
        return ref["call_key"], call, submit

    async def execute(self, ticket: str) -> dict:
        key, call, submit = self.ticket(ticket, "bash")
        async with self.file_locks.setdefault(submit.identity.key, asyncio.Lock()):
            key, call, submit = self.ticket(ticket, "bash")
            if call["status"] == "completed":
                return call["result"]
            if call["status"] != "authorized":
                raise RuntimeError("execution ticket already consumed")
            self.db.put("call", key, {**call, "status": "executing"})
            try:
                result = await self.executor.execute(submit.request_id, self.workspace(submit),
                    self.config.profiles[submit.profile_id].public_roots, call["args"]["command"],
                    call["args"].get("workdir"), timeout=min(submit.deadline_at - time.time(),
                    max(0.01, float(call["args"].get("timeout", 120000)) / 1000)))
                self.db.put("call", key, {**call, "status": "completed", "result": result})
                return result
            except BaseException:
                self.db.put("call", key, {**call, "status": "failed"})
                raise

    async def publish(self, ticket: str) -> dict:
        key, call, submit = self.ticket(ticket, "publish_attachment")
        async with self.file_locks.setdefault(submit.identity.key, asyncio.Lock()):
            key, call, submit = self.ticket(ticket, "publish_attachment")
            if call["status"] == "completed":
                return call["result"]
            files = call["args"].get("files")
            if not isinstance(files, list) or not 1 <= len(files) <= 32:
                raise ValueError("publish requires 1..32 files")
            published = []
            for index, item in enumerate(files):
                artifact_id = "att_" + digest([key, index])
                intent = self.db.get("intent", artifact_id)
                snapshot = self.config.state_root / "snapshots" / artifact_id
                if not intent:
                    source = contained(item["path"], self.workspace(submit))
                    if source.stat().st_size > self.config.limits.file_bytes:
                        raise ValueError("attachment exceeds limit")
                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    used = sum(p.stat().st_size for p in snapshot.parent.iterdir() if p.is_file())
                    if used + source.stat().st_size > self.config.limits.spool_bytes:
                        raise RuntimeError("SPOOL_FULL")
                    # All sandbox commands for this user hold the same lock. O_NOFOLLOW
                    # and fstat additionally protect the host open from a final-component swap.
                    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    import stat
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        os.close(descriptor)
                        raise PermissionError("attachment source changed")
                    with os.fdopen(descriptor, "rb") as src, snapshot.open("wb") as dst:
                        shutil.copyfileobj(src, dst, 256 * 1024)
                        dst.flush()
                        os.fsync(dst.fileno())
                    attachment = Attachment(attachment_id=artifact_id,
                        filename=item.get("filename") or source.name, size=snapshot.stat().st_size,
                        sha256=file_hash(snapshot), content_type=mimetypes.guess_type(source.name)[0]
                        or "application/octet-stream")
                    intent = {"attachment": attachment.model_dump(mode="json"), "request_id": submit.request_id}
                    self.db.put("intent", artifact_id, intent)
                attachment = Attachment.model_validate(intent["attachment"])
                while True:
                    peer = await self.connected(submit.deadline_at)
                    try:
                        result = await send_file(peer, snapshot, attachment, submit.request_id, artifact_id)
                        break
                    except (ConnectionError, TimeoutError):
                        if time.time() >= submit.deadline_at:
                            raise TimeoutError("attachment transfer deadline exceeded") from None
                        await asyncio.sleep(0.2)
                # Cancellation may have raced with an awaited transfer.
                self.active_session(call["session_id"])
                row = self.db.get("run", submit.request_id)
                if not any(a["attachment_id"] == artifact_id for a in row["artifacts"]):
                    self.db.put("run", submit.request_id, {**row, "artifacts": [*row["artifacts"], result]})
                published.append(result)
                snapshot.unlink(missing_ok=True)
            result = {"status": "queued_for_reply", "attachments": published}
            self.db.put("call", key, {**call, "status": "completed", "result": result})
            return result

    async def rpc(self, method: str, args: dict, data: bytes | None) -> dict:
        if method == "input.begin":
            row = self.db.get("run", args["request_id"])
            if not row or row["status"] != "preparing":
                raise PermissionError("input request not preparing")
            if args["transfer_id"] not in [a["attachment_id"] for a in row["submit"]["attachments"]]:
                raise PermissionError("unregistered input")
            existing = self.db.get("input_result", args["transfer_id"])
            if existing:
                if existing["request_id"] != args["request_id"]:
                    raise PermissionError("input request mismatch")
                return {"result": existing["result"]}
            return self.transfers.begin(self.config.bucket_id, args)
        if method == "input.chunk":
            return self.transfers.chunk(self.config.bucket_id, args, data)
        if method == "input.commit":
            existing = self.db.get("input_result", args["transfer_id"])
            if existing:
                return existing["result"]
            transfer, path = self.transfers.verify(self.config.bucket_id, args["transfer_id"])
            row = self.db.get("run", transfer["request_id"])
            if not row or row["status"] != "preparing":
                raise PermissionError("input request not preparing")
            submit = Submit.model_validate(row["submit"])
            target = self.workspace(submit) / "inbox" / (digest(args["transfer_id"]) + "-" + transfer["attachment"]["filename"])
            if target.is_symlink() or (target.exists() and target.stat().st_nlink != 1):
                raise PermissionError("input destination is a link")
            shutil.copyfile(path, target)
            result = {"path": str(target)}
            self.db.put("input_result", args["transfer_id"], {"request_id": submit.request_id, "result": result})
            self.transfers.release(self.config.bucket_id, args["transfer_id"])
            return result
        raise ValueError("unsupported control RPC")

    async def shutdown(self) -> None:
        self.draining = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
