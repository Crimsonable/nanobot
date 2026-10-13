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

from agent_backend.config import PermissionConfig, RuntimeConfig, load_config
from agent_backend.files import file_hash
from agent_backend.journal import Journal
from agent_backend.models import TERMINAL, Attachment, Envelope, Result, Submit, digest, uid
from agent_backend.wire import Peer
from bucket_runtime.artifact_service import ArtifactService
from bucket_runtime.opencode_client import OpenCodeClient, final_message, usage_for
from bucket_runtime.permissions import check_native_paths, public_roots, session_rules
from bucket_runtime.sandbox_executor import SandboxExecutor, contained

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, config: RuntimeConfig, client: OpenCodeClient, executor: SandboxExecutor):
        self.config, self.client, self.executor = config, client, executor
        self.permissions = (load_config(config.permissions_file, PermissionConfig)
                            if config.permissions_file else PermissionConfig())
        self.opencode_worktree = config.opencode.context_root.resolve()
        for profile_id in config.profiles:
            public_roots(config, self.permissions.defaults, profile_id)
            for user in self.permissions.users:
                if config.frontend_id and user.frontend_id not in {None, config.frontend_id}:
                    raise ValueError("permission entry belongs to another frontend")
                frontend_id = user.frontend_id or config.frontend_id
                public_roots(config, self.permissions.for_user(frontend_id, user.user_id), profile_id,
                             frontend_id)
        self.db = Journal(config.state_root / "runtime.sqlite", config.limits.outbox_messages)
        self.artifacts = ArtifactService(config, self.db)
        self.peer: Peer | None = None
        self.tasks: dict[str, asyncio.Task] = {}
        self.request_locks: dict[str, asyncio.Lock] = {}
        self.file_locks: dict[str, asyncio.Lock] = {}
        self.capacity = asyncio.Semaphore(config.limits.active)
        self.draining = False
        self.ready = False
        self.plugin_ready = False
        self.last_text_event: dict[str, float] = {}

    def policy(self, submit: Submit):
        return self.permissions.for_user(submit.identity.frontend_id, submit.identity.user_id)

    def public_roots(self, submit: Submit) -> list[Path]:
        if self.config.frontend_id and submit.identity.frontend_id != self.config.frontend_id:
            raise PermissionError("request belongs to another frontend")
        return public_roots(self.config, self.policy(submit), submit.profile_id,
                            submit.identity.frontend_id)

    def private_files(self) -> list[Path]:
        paths = [self.config.frontend_config_file, self.config.opencode.config_file,
                 self.config.permissions_file]
        if self.config.storage:
            paths.extend([self.config.storage.access_key_file, self.config.storage.secret_key_file])
        return [path for path in paths if path is not None]

    def session_permissions(self, submit: Submit) -> list[dict]:
        return session_rules(self.policy(submit), self.workspace(submit), self.public_roots(submit),
                             self.opencode_worktree, self.private_files())

    async def cancel_request(self, request_id: str) -> None:
        row = self.db.get("run", request_id)
        if not row or row["status"] in TERMINAL:
            return
        self.db.put("run", request_id, {**row, "cancel_requested": True})
        task = self.tasks.get(request_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            result = self.db.get("run", request_id).get("result") or {}
            if result.get("error") == "CANCEL_FAILED":
                raise RuntimeError("CANCEL_FAILED")
        else:
            await self.executor.kill(request_id)
            if row.get("session_id"):
                await self.client.abort(row["session_id"])
        self.finish(Result(request_id=request_id, status="cancelled"))

    async def cancel_conversation(self, submit: Submit) -> None:
        targets = []
        for row in self.db.records("run"):
            if row["request_id"] == submit.request_id or row["status"] in TERMINAL:
                continue
            other = Submit.model_validate(row["submit"])
            if other.identity == submit.identity and other.conversation_id == submit.conversation_id:
                targets.append(row["request_id"])
        for request_id in targets:
            row = self.db.get("run", request_id)
            self.db.put("run", request_id, {**row, "cancel_requested": True})
        for request_id in targets:
            await self.cancel_request(request_id)
        self.finish(Result(request_id=submit.request_id, status="completed",
                           content="已停止当前会话。" if targets else "当前会话没有正在执行的请求。"))

    def workspace(self, submit: Submit) -> Path:
        from agent_backend.workspaces import workspace_path
        if self.config.frontend_id and submit.identity.frontend_id != self.config.frontend_id:
            raise PermissionError("request belongs to another frontend")
        path = workspace_path(self.config.workspace_root, submit.identity, self.config.workspace_layout)
        for parent in [path, *path.parents]:
            if parent.is_symlink():
                raise PermissionError("workspace root is a symlink")
            if parent == self.config.workspace_root:
                break
        created = not path.exists()
        path.mkdir(parents=True, exist_ok=True)
        templates = self.config.workspace_template_root
        if created and templates and templates.is_dir():
            import shutil
            # Optional administrator-managed templates, as in the original workspace manager.
            # Never overwrite existing user files or follow template symlinks.
            for item in templates.rglob("*"):
                if item.is_symlink():
                    raise PermissionError("workspace templates must not contain symbolic links")
            for item in templates.iterdir():
                target = path / item.name
                if target.exists():
                    continue
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)
        for folder in ["output", "scratch"]:
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

    async def on_command(self, event: Envelope) -> None:
        if event.type == "request.submit":
            submit = Submit.model_validate(event.payload)
            if submit.request_id != event.request_id:
                raise ValueError("request identity mismatch")
            if self.config.frontend_id and submit.identity.frontend_id != self.config.frontend_id:
                raise PermissionError("request belongs to another frontend")
            row = self.db.get("run", submit.request_id)
            if row:
                if row["hash"] != digest(event.payload):
                    raise ValueError("request content changed")
                self.workspace(submit)
                return
            with self.db.transaction():
                self.db.put("run", submit.request_id, {"request_id": submit.request_id,
                    "submit": submit.model_dump(mode="json"), "hash": digest(event.payload),
                    "status": "queued", "artifacts": []})
                self.emit("request.accepted", submit.request_id)
            try:
                self.workspace(submit)
            except (OSError, ValueError):
                log.exception("workspace preparation failed request_id=%s", submit.request_id)
                self.finish(Result(request_id=submit.request_id, status="failed", error="WORKSPACE_UNAVAILABLE"))
                return
            if submit.content.strip().lower() == "/cancel":
                try:
                    await self.cancel_conversation(submit)
                except Exception:
                    self.draining = True
                    self.finish(Result(request_id=submit.request_id, status="failed", error="CANCEL_FAILED"))
                return
            if (self.draining or submit.profile_id not in self.config.profiles
                    or len(self.tasks) >= self.config.limits.queued + self.config.limits.active):
                self.finish(Result(request_id=submit.request_id, status="failed", error="BUCKET_UNAVAILABLE"))
            else:
                self.launch(submit.request_id)
        elif event.type == "request.cancel":
            await self.cancel_request(event.request_id)
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
                submit = Submit.model_validate(row["submit"])
                if submit.content.strip().lower() == "/cancel":
                    await self.cancel_conversation(submit)
                else:
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
                        self.opencode_worktree = Path((await self.client.paths())["worktree"])
                        session = self.db.get("session", submit.conversation_id)
                        if session and session.get("epoch", "") != submit.session_epoch:
                            session = None
                        if session:
                            if session["user_key"] != submit.identity.key or session["profile"] != submit.profile_id:
                                raise PermissionError("conversation ownership/profile mismatch")
                            session_id = session["session_id"]
                            await self.client.set_permissions(session_id, self.session_permissions(submit))
                        else:
                            session_id = await self.client.create_session(self.session_permissions(submit))
                            self.db.put("session", submit.conversation_id, {"session_id": session_id,
                                "user_key": submit.identity.key, "profile": submit.profile_id,
                                "epoch": submit.session_epoch})
                        row = self.db.get("run", request_id)
                        message_id = uid("msg_")
                        self.db.put("run", request_id, {**row, "status": "preparing",
                                    "session_id": session_id, "message_id": message_id})
                        references = []
                        image_index = 0
                        for attachment in submit.attachments:
                            alias = attachment.filename
                            if attachment.content_type.startswith("image/"):
                                image_index += 1
                                alias = f"image_{image_index} ({attachment.filename})"
                            references.append(f"{alias} — {attachment.content_type}: "
                                              + (attachment.url or attachment.path)
                                              + (" (requires authorized access)" if attachment.requires_auth else ""))
                        text = (profile.instructions + "\nYour workspace: " + str(workspace)
                            + " (access: " + self.policy(submit).filesystem.workspace + ")"
                            + "\nRead-only reference/skill directories: "
                            + json.dumps([str(p) for p in self.public_roots(submit)])
                            + "\nBash sandbox enabled: " + str(self.policy(submit).sandbox.enabled) + ". "
                            + "Use publish_attachment to include generated files in the reply. "
                            + "A published file is queued for delivery; do not claim it was already delivered.\n"
                            + "\n".join(references) + "\nUser message:\n" + submit.content)
                        if not session and submit.previous_turns:
                            text = ("Previous conversation turns (historical user/assistant content):\n"
                                    + json.dumps(submit.previous_turns, ensure_ascii=False) + "\n" + text)
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
                    self.finish(Result(request_id=request_id, status="failed", error="CANCEL_FAILED"))
                    return
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

    async def bind_tool_session(self, session_id: str) -> None:
        # Task tools create child sessions. Resolve ancestry from the trusted server,
        # never from model-provided user IDs, before issuing a sandbox/publication ticket.
        try:
            self.active_session(session_id)
            return
        except PermissionError:
            if self.db.get("child_session", session_id):
                raise
        parent = session_id
        seen = set()
        for _ in range(64):
            if parent in seen:
                break
            seen.add(parent)
            info = await self.client.session(parent)
            parent = info.get("parentID")
            if not parent:
                break
            try:
                row, _ = self.active_session(parent)
            except PermissionError:
                continue
            self.db.put("child_session", session_id, {"request_id": row["request_id"]})
            return
        raise PermissionError("session has no active managed ancestor")

    def active_session(self, session_id: str) -> tuple[dict, Submit]:
        row = next((r for r in self.db.records("run") if r.get("session_id") == session_id
                    and r["status"] == "running" and not r.get("cancel_requested")), None)
        if not row:
            child = self.db.get("child_session", session_id)
            candidate = self.db.get("run", child["request_id"]) if child else None
            if candidate and candidate["status"] == "running" and not candidate.get("cancel_requested"):
                row = candidate
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
        policy = self.policy(submit)
        normalized = check_native_paths(tool, args, policy, self.workspace(submit), self.public_roots(submit), self.private_files())
        if tool not in {"bash", "publish_attachment"}:
            return {"args": normalized}
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
                    self.public_roots(submit), call["args"]["command"],
                    call["args"].get("workdir"), timeout=min(submit.deadline_at - time.time(),
                    max(0.01, float(call["args"].get("timeout", 120000)) / 1000)),
                    sandbox=self.policy(submit).sandbox, workspace_access=self.policy(submit).filesystem.workspace,
                    private_files=self.private_files())
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
                published_attachment = await self.artifacts.upload(submit.identity, artifact_id, snapshot, attachment)
                result = published_attachment.model_dump(mode="json")
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
        raise ValueError("file transfer RPC is not supported; pass attachment references")

    async def shutdown(self) -> None:
        self.draining = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
