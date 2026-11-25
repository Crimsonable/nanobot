import pytest
from pydantic import ValidationError

from agent_backend.journal import Journal
from agent_backend.models import Attachment, Identity


def test_identity_and_filename_boundaries():
    assert (
        Identity(frontend_id="a/b", user_id="c").identity_id
        != Identity(frontend_id="a-b", user_id="c").identity_id
    )
    for name in ["../x", "a/b", "a\\b", "..", "bad\n.txt"]:
        with pytest.raises(ValidationError):
            Attachment(filename=name)
    with pytest.raises(ValidationError):
        Identity(frontend_id="f", user_id="u", admin=True)


def test_journal_transactions_survive_restart_without_a_transport_outbox(tmp_path):
    db = Journal(tmp_path / "state.db")
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.put("request", "r", {"status": "completed"})
            db.accept("native", "event")
            raise RuntimeError("crash")
    assert db.get("request", "r") is None and not db.seen("native", "event")
    with db.transaction():
        db.put("request", "r", {"status": "completed"})
        db.accept("native", "event")
    with pytest.raises(RuntimeError, match="active writer"):
        Journal(tmp_path / "state.db")
    db.close()
    db = Journal(tmp_path / "state.db")
    assert db.get("request", "r")["status"] == "completed" and db.seen("native", "event")
    assert not db.db.execute("SELECT 1 FROM sqlite_master WHERE name='outbox'").fetchone()
    db.close()
