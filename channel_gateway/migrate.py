"""Offline import of drained legacy CU delivery records. Source database is read-only."""
import argparse
import json
import sqlite3
from pathlib import Path

from agent_backend.ingress import Inbound
from agent_backend.journal import Journal
from agent_backend.models import digest


def migrate(source: Path, destination: Path) -> int:
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as old:
        rows = [json.loads(r[0]) for r in old.execute("SELECT value FROM records WHERE kind='request'")]
        if any(not row.get("result") for row in rows):
            raise ValueError("finish or cancel all legacy requests before migration")
        if old.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]:
            raise ValueError("drain legacy CU outbox before migration")
    db = Journal(destination)
    imported = 0
    try:
        with db.transaction():
            for row in rows:
                if db.get("request", row["request_id"]):
                    continue
                submit = row["submit"]
                identity = submit["identity"]
                body = Inbound(request_id=row["request_id"], user_id=identity["user_id"],
                    chat_id=row["chat_id"], content=submit["content"], attachments=submit["attachments"],
                    metadata=row["metadata"]).model_dump()
                value = {**row, "legacy_hash": row["hash"], "frontend_id": identity["frontend_id"], "input": body,
                         "hash": digest({"frontend": identity["frontend_id"], "input": body, "sync": row["sync"]}),
                         "delivery": "unknown" if row["delivery"] == "sending" else row["delivery"]}
                db.put("request", row["request_id"], value)
                imported += 1
    finally:
        db.close()
    return imported


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    if args.source.resolve() == args.destination.resolve():
        parser.error("source and destination must differ")
    print(f"Imported {migrate(args.source, args.destination)} delivery records.")


if __name__ == "__main__":
    main()
