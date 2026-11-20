"""Frontend session catalog and restartable native transcript transfers."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from pathlib import Path

from agent_backend.config import ControlConfig
from agent_backend.journal import Journal
from agent_backend.models import TERMINAL, Envelope, ExecutionStatus, Identity, Submit
from agent_backend.session_history import SessionBusy, transfer_path

SourceCall = Callable[[dict], Awaitable[None]]


class SessionManager:
    def __init__(self, config: ControlConfig, db: Journal,
                 open_source: Callable[[str], AbstractContextManager[SourceCall]]):
        self.config = config
        self.db = db
        self.open_source = open_source

    def record(self, submit: Submit, bucket_id: str, session_id: str) -> None:
        self.db.put("session", submit.session_key, {"identity": submit.identity.model_dump(),
                    "chat_id": submit.chat_id, "session_id": session_id, "bucket_id": bucket_id})

    def apply_request_event(self, event: Envelope) -> str | None:
        """Return the identity to release when a request first becomes terminal."""
        row = self.db.get("request", event.request_id)
        if not row or row["bucket_id"] != event.bucket_id:
            raise PermissionError("request does not belong to this bucket")
        namespace, _, status = event.type.partition(".")
        if namespace != "request":
            raise ValueError("unsupported bucket event")
        if status in {"accepted", "started"}:
            if not row.get("result"):
                if status == "started" and (session_id := event.payload.get("session_id")):
                    self.record(Submit.model_validate(row["submit"]), event.bucket_id, session_id)
                self.db.put("request", event.request_id, {**row, "status": status})
            return None
        if status not in TERMINAL:
            raise ValueError("unsupported bucket event")
        result = ExecutionStatus.model_validate(event.payload)
        if result.request_id != event.request_id or result.status != status:
            raise ValueError("terminal identity mismatch")
        if row.get("result"):
            if row["result"] != result.model_dump(mode="json"):
                raise ValueError("conflicting terminal result")
            return None
        return self._finish(row, result)

    def _finish(self, row: dict, result: ExecutionStatus) -> str:
        self.db.put("request", result.request_id, {**row, "status": result.status,
                    "result": result.model_dump(mode="json")})
        return Identity.model_validate(row["submit"]["identity"]).identity_id

    def interrupt_requests(self, bucket_id: str) -> set[str]:
        self.db.db.execute("DELETE FROM outbox WHERE peer=?", (bucket_id,))
        released = set()
        for row in self.db.records("request"):
            if row["bucket_id"] == bucket_id and not row.get("result"):
                result = ExecutionStatus(request_id=row["request_id"], status="failed",
                                         error="BUCKET_MISSING_EXECUTION_INTERRUPTED")
                released.add(self._finish(row, result))
        return released

    def root(self, identity: Identity, bucket_id: str) -> Path:
        bucket = self.db.get("bucket", bucket_id)
        if not bucket or bucket["frontend_id"] != identity.frontend_id:
            raise PermissionError("session bucket frontend mismatch")
        if bucket_id in {".", ".."} or bucket_id != bucket["bucket_id"] or any(c in bucket_id for c in "/\\"):
            raise ValueError("invalid bucket ID")
        return self.config.runtime_for(identity.frontend_id).state_root / identity.frontend_id / bucket_id

    def lookup(self, submit):
        return self.db.get("session", submit.session_key)

    async def prepare(self, submit: Submit, target_bucket: str) -> str | None:
        if submit.content.strip().lower() == "/cancel":
            return None
        # A retry may arrive in another chat. Finish this user's recorded move
        # before allowing any new prompt into the reserved destination.
        for pending in self.db.records("migration"):
            if pending["identity_id"] == submit.identity.identity_id and pending["chat_id"] != submit.chat_id:
                await self.prepare(submit.model_copy(update={"chat_id": pending["chat_id"]}), target_bucket)
        catalog = self.lookup(submit)
        if catalog is None:
            return None
        if catalog["identity"] != submit.identity.model_dump() or catalog["chat_id"] != submit.chat_id:
            raise PermissionError("session catalog ownership mismatch")
        session_id, source_bucket = catalog["session_id"], catalog["bucket_id"]
        movement = self.db.get("migration", submit.session_key)
        if source_bucket == target_bucket and movement is None:
            return session_id
        if movement is None:
            movement = {"identity_id": submit.identity.identity_id, "source": source_bucket,
                        "target": target_bucket, "chat_id": submit.chat_id, "step": "export"}
            self.db.put("migration", submit.session_key, movement)
        if movement["source"] != source_bucket or movement["target"] != target_bucket:
            raise RuntimeError("resume the recorded migration target before placing new work")
        path = transfer_path(self.root(submit.identity, source_bucket), source_bucket, submit.session_key)
        self.root(submit.identity, target_bucket)  # Validate both bucket owners.
        payload = {"identity": submit.identity.model_dump(), "chat_id": submit.chat_id,
                   "session_id": session_id, "source_bucket": source_bucket}
        with self.open_source(source_bucket) as source, self.open_source(target_bucket) as target:
            if movement["step"] == "export":
                try:
                    await source({**payload, "operation": "export"})
                except RuntimeError as exc:
                    if str(exc).startswith("SESSION_BUSY"):
                        self.db.delete("migration", submit.session_key)
                        raise SessionBusy(source_bucket) from exc
                    raise
                movement = {**movement, "step": "import"}
                self.db.put("migration", submit.session_key, movement)
            # Re-verify even after a restart/lost delete reply, before removing the source.
            await target({**payload, "operation": "import"})
            self.db.put("migration", submit.session_key, {**movement, "step": "delete"})
            await source({**payload, "operation": "delete"})
            with self.db.transaction():
                self.record(submit, target_bucket, session_id)
                self.db.delete("migration", submit.session_key)
            path.unlink(missing_ok=True)
        return session_id
