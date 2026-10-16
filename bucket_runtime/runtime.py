from __future__ import annotations

import asyncio
import json
import logging
import math
import mimetypes
import os
import secrets
import shutil
import time
from pathlib import Path

import httpx

from agent_backend.config import PermissionConfig, RuntimeConfig, load_config
from agent_backend.files import file_hash
from agent_backend.journal import Journal
from agent_backend.models import TERMINAL, Attachment, Envelope, Result, Submit, digest, uid
from agent_backend.wire import Peer
from bucket_runtime.artifact_service import ArtifactService
from bucket_runtime.file_lock import publication_lock
from bucket_runtime.opencode_client import OpenCodeClient
from bucket_runtime.permissions import check_native_paths, public_roots, session_rules
from bucket_runtime.sandbox_executor import SandboxExecutor, contained

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, config: RuntimeConfig, client: OpenCodeClient, executor: SandboxExecutor):
        self.config, self.client, self.executor = config, client, executor
        self.permissions = (load_config(config.permissions_file, PermissionConfig)
                            if config.permissions_file else PermissionConfig())
        self.opencode_worktrees: dict[Path, Path] = {}
        public_roots(config, self.permissions.defaults)
        for user in self.permissions.users:
            if user.frontend_id not in {None, config.frontend_id}:
                raise ValueError("permission entry belongs to another frontend")
            public_roots(config, self.permissions.for_user(config.frontend_id, user.user_id))
        self.db = Journal(config.state_root / "runtime.sqlite", config.limits.outbox_messages)
        # Remove the obsolete derived transcript cache. OpenCode owns history.
        self.db.db.execute("DELETE FROM records WHERE kind='message'")
        self.artifacts = ArtifactService(config, self.db)
        self.peer: Peer | None = None
        self.tasks: dict[str, asyncio.Task] = {}
        self.request_locks: dict[str, asyncio.Lock] = {}
        self.file_locks: dict[str, asyncio.Lock] = {}
        self.capacity = asyncio.Semaphore(config.limits.active)
        self.draining = False
        self.ready = False
        self.plugin_ready = False
        self.completions: dict[str, asyncio.Event] = {}
        from bucket_runtime.plugin_events import PluginEvents
        self.plugin_events = PluginEvents(self)

    def policy(self, submit: Submit):
        return self.permissions.for_user(submit.identity.frontend_id, submit.identity.user_id)

    def user_lock_path(self, submit: Submit) -> Path:
        directory = self.config.state_root / "execution-locks"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / (submit.identity.identity_id + ".lock")

    def public_roots(self, submit: Submit) -> list[Path]:
        if submit.identity.frontend_id != self.config.frontend_id:
            raise PermissionError("request belongs to another frontend")
        return public_roots(self.config, self.policy(submit))

    def private_files(self) -> list[Path]:
        paths = [self.config.frontend_config_file, self.config.opencode.config_file,
                 self.config.permissions_file, self.config.logging_file]
        if self.config.storage:
            paths.extend([self.config.storage.access_key_file, self.config.storage.secret_key_file])
        return [path for path in paths if path is not None]

    def session_permissions(self, submit: Submit) -> list[dict]:
        return session_rules(self.policy(submit), self.workspace(submit), self.public_roots(submit),
                             self.worktree(submit), self.private_files())

    def worktree(self, submit: Submit) -> Path:
        workspace = self.workspace(submit)
        return self.opencode_worktrees.get(workspace, workspace)

    def run_client(self, row: dict) -> OpenCodeClient:
        directory = Path(row["directory"])
        return self.client.for_directory(directory)

    def session_context(self, submit: Submit, message_id: str) -> dict:
        return {"schema_version": 1, **submit.identity.model_dump(),
                "identity_id": submit.identity.identity_id, "bucket_id": self.config.bucket_id,
                "chat_id": submit.chat_id,
                "input_message_id": message_id,
                "requests": {message_id: submit.request_id}}

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
            if row.get("session_id"):
                await self.run_client(row).abort(row["session_id"])
        self.finish(Result(request_id=request_id, status="cancelled"))

    async def cancel_conversation(self, submit: Submit) -> None:
        targets = []
        for row in self.db.records("run"):
            if row["request_id"] == submit.request_id or row["status"] in TERMINAL:
                continue
            other = Submit.model_validate(row["submit"])
            if other.identity == submit.identity and other.session_key == submit.session_key:
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
        if submit.identity.frontend_id != self.config.frontend_id:
            raise PermissionError("request belongs to another frontend")
        path = workspace_path(self.config.workspace_root, submit.identity)
        for parent in [path, *path.parents]:
            if parent.is_symlink():
                raise PermissionError("workspace root is a symlink")
            if parent == self.config.workspace_root:
                break
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def emit(self, kind: str, request_id: str, payload: dict | None = None) -> None:
        self.db.enqueue(self.config.bucket_id, Envelope(type=kind, bucket_id=self.config.bucket_id,
                        request_id=request_id, payload=payload or {}))

    async def session_operation(self, payload: dict) -> None:
        """CU holds the user lock and never migrates an outstanding request."""
        from agent_backend.models import Identity
        from agent_backend.session_history import forget_local_session
        identity = Identity.model_validate(payload["identity"])
        if identity.frontend_id != self.config.frontend_id:
            raise PermissionError("session belongs to another frontend")
        session_id = payload["session_id"]
        key = digest([identity.identity_id, payload["chat_id"]])
        async with self.request_locks.setdefault(identity.identity_id, asyncio.Lock()):
            if any(row["status"] not in TERMINAL and row["submit"]["identity"] == identity.model_dump()
                   for row in self.db.records("run")):
                raise RuntimeError("user still has outstanding requests")
            from agent_backend.workspaces import workspace_path
            client = self.client.for_directory(workspace_path(self.config.workspace_root, identity))
            try:
                info = await client.session(session_id)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                info = None
            if info is not None:
                context = info.get("metadata", {}).get("bucket_runtime", {})
                if any(context.get(k) != v for k, v in {
                    **identity.model_dump(), "chat_id": payload["chat_id"],
                }.items()):
                    raise PermissionError("session ownership mismatch")
                if Path(info["directory"]).resolve() != client.directory:
                    raise PermissionError("session workspace mismatch")
            if payload["operation"] == "quiesce":
                if info is None:
                    return  # A retried delete may already have removed the source.
                sessions = [session_id]
                for current in sessions:
                    await client.abort(current)
                    sessions.extend(child["id"] for child in await client.children(current)
                                    if child["id"] not in sessions)
            elif payload["operation"] == "delete":
                if info is not None:
                    await client.delete_session(session_id)
                forget_local_session(self.db, key, payload["session_ids"])
            else:
                raise ValueError("unsupported session operation")

    def finish(self, result: Result, native_reply: dict | None = None) -> None:
        row = self.db.get("run", result.request_id)
        if not row or row["status"] in TERMINAL:
            return
        with self.db.transaction():
            self.db.put("run", result.request_id, {**row, "status": result.status,
                         "result": result.model_dump(mode="json")})
            self.emit("request." + result.status, result.request_id, {**result.model_dump(mode="json"),
                **({"native_reply": native_reply} if native_reply else {})})
        if completed := self.completions.get(result.request_id):
            completed.set()
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
            if submit.identity.frontend_id != self.config.frontend_id:
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
            if (self.draining
                    or len(self.tasks) >= self.config.limits.queued + self.config.limits.active):
                self.finish(Result(request_id=submit.request_id, status="failed", error="BUCKET_UNAVAILABLE"))
            else:
                self.launch(submit.request_id)
        elif event.type == "request.cancel":
            await self.cancel_request(event.request_id)
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
                    client = self.run_client(row)
                    await client.abort(row["session_id"])
                self.finish(Result(request_id=row["request_id"], status="failed",
                    error="EXECUTION_INTERRUPTED", partial=bool(row["artifacts"]), attachments=row["artifacts"]))

    async def run(self, request_id: str) -> None:
        row = self.db.get("run", request_id)
        submit = Submit.model_validate(row["submit"])
        session_id = None
        try:
            async with asyncio.timeout(max(0.01, submit.deadline_at - time.time())):
                async with self.request_locks.setdefault(submit.identity.identity_id, asyncio.Lock()):
                    async with self.capacity:
                        if self.draining or self.db.get("run", request_id).get("cancel_requested"):
                            raise asyncio.CancelledError()
                        workspace = self.workspace(submit)
                        client = self.client.for_directory(workspace)
                        self.opencode_worktrees[workspace] = Path((await client.paths())["worktree"])
                        session = self.db.get("session", submit.session_key)
                        if submit.session_id:
                            session = {"session_id": submit.session_id,
                                       "identity_id": submit.identity.identity_id}
                        message_id = uid("msg_")
                        context = self.session_context(submit, message_id)
                        if session:
                            if session["identity_id"] != submit.identity.identity_id:
                                raise PermissionError("conversation ownership mismatch")
                            session_id = session["session_id"]
                            info = await client.session(session_id)
                            if Path(info["directory"]).resolve() != workspace:
                                session_id = None
                                raise RuntimeError("SESSION_WORKSPACE_MISMATCH: existing chat belongs to another directory; start a new chat")
                            client.check_permissions(info, self.session_permissions(submit))
                            await client.set_context(session_id, context, info)
                        else:
                            session_id = await client.create_session(self.session_permissions(submit), context)
                        self.db.put("session", submit.session_key, {"session_id": session_id,
                            "identity_id": submit.identity.identity_id, "directory": str(workspace)})
                        row = self.db.get("run", request_id)
                        self.db.put("run", request_id, {**row, "status": "preparing",
                                    "session_id": session_id, "message_id": message_id, "directory": str(workspace)})
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
                        text = ("Your workspace: " + str(workspace)
                            + " (access: " + self.policy(submit).filesystem.workspace + ")"
                            + "\nRead-only reference/skill directories: "
                            + json.dumps([str(p) for p in self.public_roots(submit)])
                            + "\nBash sandbox enabled: " + str(self.policy(submit).sandbox.enabled) + ". "
                            + "Use publish_attachment to include generated files in the reply. "
                            + "A published file is queued for delivery; do not claim it was already delivered.\n"
                            + "\n".join(references) + "\nUser message:\n" + submit.content)
                        row = self.db.get("run", request_id)
                        with self.db.transaction():
                            self.db.put("run", request_id, {**row, "status": "running"})
                            self.emit("request.started", request_id, {"session_id": session_id})
                        completed = self.completions.setdefault(request_id, asyncio.Event())
                        try:
                            await client.submit(session_id, message_id, text)
                        except httpx.TransportError:
                            # Submission may have reached OpenCode. Await the plugin;
                            # never query messages or resubmit an uncertain request.
                            log.warning("prompt submission uncertain request_id=%s; awaiting plugin", request_id)
                        await completed.wait()
        except asyncio.CancelledError:
            if session_id:
                try:
                    await client.abort(session_id)
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
            try:
                if session_id:
                    await client.abort(session_id)
            except Exception:
                self.draining = True
            finally:
                row = self.db.get("run", request_id)
                self.finish(Result(request_id=request_id, status="failed", error=type(exc).__name__
                    if not isinstance(exc, (RuntimeError, TimeoutError)) else str(exc) or "REQUEST_TIMEOUT",
                    attachments=row["artifacts"], partial=bool(row["artifacts"])))

        finally:
            self.completions.pop(request_id, None)

    def plugin_event(self, body: dict) -> dict:
        return self.plugin_events.accept(body)

    def reduce_event(self, event) -> None:
        """Track execution completion only; CU resolves reply content from native events."""
        native = event.event
        kind, props = native["type"], native.get("properties", {})
        if kind == "session.error" and event.request_id:
            row = self.db.get("run", event.request_id)
            if row["status"] == "running" and not row.get("cancel_requested"):
                self.finish(Result(request_id=event.request_id, status="failed",
                    error=props.get("error", {}).get("name", "MODEL_ERROR"), attachments=row["artifacts"]))
            return
        if kind != "message.updated" or not event.request_id:
            return
        info = props.get("info", {})
        if (info.get("role") != "assistant" or info.get("summary")
                or not info.get("time", {}).get("completed")):
            return
        row = self.db.get("run", event.request_id)
        if row["status"] != "running" or row.get("cancel_requested"):
            return
        error = info.get("error", {}).get("name")
        reason = info.get("finish")
        if not error and reason not in {"stop", "end_turn", "length", "content-filter"}:
            return
        error = error or (None if reason in {"stop", "end_turn"} else "MODEL_" + reason.upper())
        # Only execution state lives here. CU already receives the full native
        # snapshots and uses those to construct the channel reply.
        self.finish(Result(request_id=event.request_id, status="failed" if error else "completed",
            error=error, attachments=row["artifacts"], partial=bool(error and row["artifacts"])),
            native_reply={"session_id": event.session_id, "input_message_id": row["message_id"]})

    async def bind_tool_session(self, session_id: str, directory: str) -> None:
        # Task tools create child sessions. Resolve ancestry from the trusted server,
        # never from model-provided user IDs, before authorizing tools.
        requested = Path(directory).resolve()

        def check_directory(submit: Submit) -> None:
            if requested != self.workspace(submit):
                raise PermissionError("tool session workspace mismatch")

        try:
            _, submit = self.active_session(session_id)
        except PermissionError:
            if self.db.get("child_session", session_id):
                raise
        else:
            check_directory(submit)
            return
        if not any(r["status"] == "running" and self.workspace(Submit.model_validate(r["submit"])) == requested
                   for r in self.db.records("run")):
            raise PermissionError("session has no active authorized workspace")
        client = self.client.for_directory(requested)
        parent = session_id
        seen = set()
        for _ in range(64):
            if parent in seen:
                break
            seen.add(parent)
            info = await client.session(parent)
            if Path(info["directory"]).resolve() != requested:
                raise PermissionError("tool session workspace mismatch")
            parent = info.get("parentID")
            if not parent:
                break
            try:
                row, submit = self.active_session(parent)
            except PermissionError:
                continue
            check_directory(submit)
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

    def authorize(self, session_id: str, call_id: str, tool: str, args: dict) -> dict:
        row, submit = self.active_session(session_id)
        policy = self.policy(submit)
        normalized = check_native_paths(tool, args, policy, self.workspace(submit), self.public_roots(submit), self.private_files())
        if tool not in {"bash", "publish_attachment"}:
            return {"args": normalized}
        if tool == "bash":
            command = args.get("command")
            if not isinstance(command, str) or len(command) > 100000 or "\0" in command:
                raise ValueError("invalid shell command")
            timeout = args.get("timeout", 120000)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("invalid shell timeout")
            remaining = (submit.deadline_at - time.time()) * 1000
            if remaining <= 0:
                raise PermissionError("request deadline expired")
            if args.get("workdir") is not None and not isinstance(args["workdir"], str):
                raise ValueError("invalid working directory")
            workspace, public = self.workspace(submit), self.public_roots(submit)
            cwd = self.executor.working_directory(workspace, public, args.get("workdir"), policy.filesystem.workspace)
            # Return only trusted launch arguments. The plugin appends the original
            # command as one quoted argument; no command execution RPC or ticket.
            argv = self.executor.argv(workspace, public, cwd, "", sandbox=policy.sandbox,
                workspace_access=policy.filesystem.workspace, private_files=self.private_files())[:-1]
            # flock stays outside the sandbox; --close prevents exposing its descriptor.
            argv = ["/usr/bin/flock", "--exclusive", "--close", str(self.user_lock_path(submit)), *argv]
            return {"argv": argv, "host_workdir": str(workspace),
                    "timeout_ms": max(1, math.ceil(min(timeout, remaining)))}
        call_key = digest([session_id, call_id])
        old = self.db.get("call", call_key)
        if old:
            if old["tool"] != tool or old["args"] != args or old["request_id"] != submit.request_id:
                raise PermissionError("tool call identity changed")
            return {"ticket": old["ticket"]}
        ticket = secrets.token_urlsafe(32)
        call = {"ticket": ticket, "request_id": submit.request_id, "session_id": session_id,
                "call_id": call_id, "tool": tool, "args": args, "status": "authorized"}
        with self.db.transaction():
            self.db.put("call", call_key, call)
            self.db.put("ticket", ticket, {"call_key": call_key})
        return {"ticket": ticket}

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

    async def publish(self, ticket: str) -> dict:
        key, call, submit = self.ticket(ticket, "publish_attachment")
        async with self.file_locks.setdefault(submit.identity.identity_id, asyncio.Lock()), publication_lock(self.user_lock_path(submit)):
            key, call, submit = self.ticket(ticket, "publish_attachment")
            if call["status"] == "completed":
                return call["result"]
            files = call["args"].get("files")
            if not isinstance(files, list) or not 1 <= len(files) <= 32:
                raise ValueError("publish requires 1..32 files")
            published = []
            for index, item in enumerate(files):
                attachment_id = "att_" + digest([key, index])
                intent = self.db.get("intent", attachment_id)
                snapshot = self.config.state_root / "snapshots" / attachment_id
                if not intent:
                    source = contained(item["path"], self.workspace(submit))
                    if source.stat().st_size > self.config.limits.file_bytes:
                        raise ValueError("attachment exceeds limit")
                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    used = sum(p.stat().st_size for p in snapshot.parent.iterdir() if p.is_file())
                    if used + source.stat().st_size > self.config.limits.spool_bytes:
                        raise RuntimeError("SPOOL_FULL")
                    # Native commands hold the same OS lock outside bwrap. O_NOFOLLOW
                    # and fstat additionally protect the final component of the host open.
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
                    attachment = Attachment(attachment_id=attachment_id,
                        filename=item.get("filename") or source.name, size=snapshot.stat().st_size,
                        sha256=file_hash(snapshot), content_type=mimetypes.guess_type(source.name)[0]
                        or "application/octet-stream")
                    intent = {"attachment": attachment.model_dump(mode="json"), "request_id": submit.request_id}
                    self.db.put("intent", attachment_id, intent)
                attachment = Attachment.model_validate(intent["attachment"])
                published_attachment = await self.artifacts.upload(submit.identity, attachment_id, snapshot, attachment)
                result = published_attachment.model_dump(mode="json")
                # Cancellation may have raced with an awaited transfer.
                self.active_session(call["session_id"])
                row = self.db.get("run", submit.request_id)
                if not any(a["attachment_id"] == attachment_id for a in row["artifacts"]):
                    self.db.put("run", submit.request_id, {**row, "artifacts": [*row["artifacts"], result]})
                published.append(result)
                snapshot.unlink(missing_ok=True)
            result = {"status": "queued_for_reply", "attachments": published}
            self.db.put("call", key, {**call, "status": "completed", "result": result})
            return result

    async def shutdown(self) -> None:
        self.draining = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
