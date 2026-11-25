"""Business session catalog and restartable transfers through native APIs."""
from __future__ import annotations

from pathlib import Path

from agent_backend.config import ControlConfig
from agent_backend.journal import Journal
from agent_backend.models import ExecutionStatus, Submit, uid
from agent_backend.session_history import (
    SessionBusyError,
    check_owner,
    read_history,
    verify_history,
    write_history,
)
from container_up.migrations import upgrade_native_owner


class SessionManager:
    def __init__(self, config: ControlConfig, db: Journal, pool):
        self.config, self.db, self.pool = config, db, pool

    def record(self, submit: Submit, bucket_id: str, session_id: str, *, provisioned=True) -> None:
        self.db.put("session", submit.session_key, {"identity": submit.identity.model_dump(),
            "chat_id": submit.chat_id, "session_id": session_id, "bucket_id": bucket_id, "provisioned": provisioned})

    def finish(self, row: dict, result: ExecutionStatus) -> str:
        self.db.put("request", result.request_id, {**row, "status": result.status,
                    "result": result.model_dump(mode="json")})
        return Submit.model_validate(row["submit"]).session_key

    def interrupt_requests(self, bucket_id: str) -> set[str]:
        released = set()
        for row in self.db.records("request"):
            if row["bucket_id"] == bucket_id and not row.get("result"):
                released.add(self.finish(row, ExecutionStatus(request_id=row["request_id"],
                    status="failed", error="BUCKET_MISSING_EXECUTION_INTERRUPTED")))
        return released

    async def prepare(self, submit: Submit, target_bucket: str, workspace: Path) -> str:
        catalog = self.db.get("session", submit.session_key)
        existing = catalog is not None
        if catalog is None:
            # Persist the ID before HTTP creation. A lost reply cannot orphan another session.
            self.record(submit, target_bucket, uid("ses_"), provisioned=False)
            catalog = self.db.get("session", submit.session_key)
        if catalog["identity"] != submit.identity.model_dump() or catalog["chat_id"] != submit.chat_id:
            raise PermissionError("session catalog ownership mismatch")
        session_id, source_bucket = catalog["session_id"], catalog["bucket_id"]
        owner = {**submit.identity.model_dump(), "identity_id": submit.identity.identity_id,
                 "chat_id": submit.chat_id}
        movement = self.db.get("migration", submit.session_key)
        if existing and not movement:
            if source_bucket != target_bucket:
                await self.pool.ready(source_bucket, recover=True)
            found = await upgrade_native_owner(await self.pool.client(source_bucket), session_id, owner)
            if not found and catalog.get("provisioned") is False:
                self.record(submit, target_bucket, session_id, provisioned=False)
                catalog = self.db.get("session", submit.session_key)
                source_bucket = target_bucket
        if source_bucket != target_bucket or movement:
            await self.transfer(submit, catalog, target_bucket, workspace, movement)
        client = await self.pool.client(target_bucket)
        await client.create_session(session_id, workspace, owner)
        self.record(submit, target_bucket, session_id)
        return session_id

    async def transfer(self, submit, catalog, target_bucket, workspace, movement):
        source_bucket, session_id = catalog["bucket_id"], catalog["session_id"]
        if movement is None:
            movement = {"source": source_bucket, "target": target_bucket, "step": "export"}
            self.db.put("migration", submit.session_key, movement)
        if movement["source"] != source_bucket or movement["target"] != target_bucket:
            raise RuntimeError("resume the recorded migration target before placing new work")
        for bucket_id in (source_bucket, target_bucket):
            if self.db.get("bucket", bucket_id)["frontend_id"] != submit.identity.frontend_id:
                raise PermissionError("session bucket frontend mismatch")
        path = self.config.state_root / "session-transfers" / (submit.session_key + ".json")
        with self.pool.hold(source_bucket), self.pool.hold(target_bucket):
            await self.pool.ready(source_bucket, recover=True)
            await self.pool.ready(target_bucket, recover=True)
            source, target = await self.pool.client(source_bucket), await self.pool.client(target_bucket)
            if movement["step"] == "export":
                tree = await source.tree(session_id)
                check_owner(tree[0], submit.identity, submit.chat_id)
                pending = any(not r.get("result") and Submit.model_validate(r["submit"]).session_key == submit.session_key
                              for r in self.db.records("request"))
                if pending or not await source.idle(tree):
                    self.db.delete("migration", submit.session_key)
                    raise SessionBusyError(source_bucket)
                write_history(path, [await source.export_session(info["id"]) for info in tree])
                movement = {**movement, "step": "import"}
                self.db.put("migration", submit.session_key, movement)
            history = read_history(path, session_id, submit.identity, submit.chat_id)
            target_tree = await target.tree(session_id, missing_ok=True)
            if target_tree and not await target.idle(target_tree):
                raise RuntimeError("migration target has pending work")
            for data in history:
                expected = {**data, "info": {**data["info"], "location": {"directory": str(workspace)}}}
                await target.import_session(expected, workspace)
                verify_history(expected, await target.export_session(data["info"]["id"]))
            source_tree = await source.tree(session_id, missing_ok=True)
            if source_tree:
                if not await source.idle(source_tree):
                    raise RuntimeError("migration source has pending work")
                expected_by_id = {data["info"]["id"]: data for data in history}
                for info in source_tree:
                    if info["id"] not in expected_by_id:
                        raise RuntimeError("migration source tree changed")
                    verify_history(expected_by_id[info["id"]], await source.export_session(info["id"]))
            # Every descendant must survive readback before any source is deleted.
            self.db.put("migration", submit.session_key, {**movement, "step": "delete"})
            for data in reversed(history):
                await source.delete_session(data["info"]["id"])
            with self.db.transaction():
                self.record(submit, target_bucket, session_id)
                self.db.delete("migration", submit.session_key)
            path.unlink(missing_ok=True)
