import pytest

from agent_backend.journal import Journal
from scripts.migrate_backend_workspaces import migrate


def test_migration_dry_run_copy_and_refuse_overwrite(tmp_path):
    old = tmp_path / "old"
    source = old / "alice"
    source.mkdir(parents=True)
    (source / "report.txt").write_text("existing report")
    db = Journal(tmp_path / "control.sqlite")
    manifest = [{"frontend_id": "web", "user_id": "alice", "bucket_id": "b",
                 "source_workspace": str(source)}]
    planned = migrate(manifest, old, tmp_path / "new", db)
    assert not (tmp_path / "new").exists()
    migrated = migrate(manifest, old, tmp_path / "new", db, True)
    assert migrated == planned
    assert (source / "report.txt").read_text() == "existing report"
    with pytest.raises(ValueError):
        migrate(manifest, old, tmp_path / "new", db, True)
    db.close()


def test_single_writer_is_enforced(tmp_path):
    db = Journal(tmp_path / "state.sqlite")
    try:
        with pytest.raises(RuntimeError, match="active writer"):
            Journal(tmp_path / "state.sqlite")
    finally:
        db.close()
