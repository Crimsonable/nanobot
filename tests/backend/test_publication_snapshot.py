"""The trusted plugin's local helper uses secure local file descriptors and locks."""

import importlib.util
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="requires Linux openat and flock")


@pytest.fixture
def publication(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "publication_snapshot", Path(__file__).resolve().parents[2] / "opencode_plugin/snapshot.py"
    )
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    workspace = tmp_path / "web/alice"
    workspace.mkdir(parents=True)
    (workspace / "report.txt").write_bytes(b"report")
    body = {
        "scope": {
            "behavior": "pool",
            "workspace": str(workspace),
            "lock_path": str(tmp_path / "lock"),
            "deadline_at": time.time() + 60,
            "file_bytes": 100,
            "spool_bytes": 100,
        },
        "root": str(tmp_path / "publications"),
        "key": "one",
        "files": [{"path": "report.txt"}],
    }
    return helper, body, workspace


def test_snapshot_retries_preserve_original_bytes(publication):
    helper, body, workspace = publication
    first = helper.snapshot(body)
    (workspace / "report.txt").write_bytes(b"changed")
    assert helper.snapshot(body) == first
    assert Path(first[0]["path"]).read_bytes() == b"report"


@pytest.mark.parametrize(
    "kind", ["other_frontend", "symlink", "hardlink", "file_limit", "quota", "expired"]
)
def test_snapshot_rejects_unsafe_or_excessive_input(publication, tmp_path, kind):
    helper, body, workspace = publication
    if kind in {"other_frontend", "symlink", "hardlink"}:
        outside = tmp_path / "other/alice/secret"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"secret")
        if kind == "other_frontend":
            body["files"] = [{"path": str(outside)}]
        else:
            target = workspace / "link"
            if kind == "symlink":
                target.symlink_to(outside)
            else:
                target.hardlink_to(outside)
            body["files"] = [{"path": "link"}]
    elif kind == "file_limit":
        body["scope"]["file_bytes"] = 2
    elif kind == "quota":
        helper.snapshot(body)
        body.update(key="two")
        body["scope"]["spool_bytes"] = 10
    else:
        body["scope"]["deadline_at"] = 0
    with pytest.raises((PermissionError, ValueError)):
        helper.snapshot(body)


def test_expired_snapshot_releases_quota(publication):
    helper, body, _ = publication
    old = helper.snapshot(body)
    (Path(body["root"]) / "one/expires").write_text("0")
    body["key"] = "two"
    body["scope"]["spool_bytes"] = 6
    assert helper.snapshot(body)[0]["size"] == 6
    assert not Path(old[0]["path"]).exists()


def test_interrupted_copy_is_cleaned_under_quota_lock(publication):
    helper, body, _ = publication
    orphan = Path(body["root"]) / "aborted"
    orphan.mkdir(parents=True)
    (orphan / "0.data").write_bytes(b"x" * 100)
    assert helper.snapshot(body)[0]["size"] == 6
    assert not orphan.exists()


def test_individual_snapshot_needs_no_cooperating_shell_lock(publication):
    helper, body, _ = publication
    body["scope"].pop("lock_path")
    body["scope"]["behavior"] = "individual"
    result = helper.snapshot(body)
    assert Path(result[0]["path"]).read_bytes() == b"report"


def test_individual_snapshot_rejects_source_changed_during_copy(publication, monkeypatch):
    from types import SimpleNamespace

    helper, body, _ = publication
    body["scope"].pop("lock_path")
    body["scope"]["behavior"] = "individual"
    original, calls = helper.os.fstat, 0

    def changed(fd):
        nonlocal calls
        calls += 1
        value = original(fd)
        if calls == 2:
            return SimpleNamespace(st_size=value.st_size + 1, st_mtime_ns=value.st_mtime_ns,
                                   st_ctime_ns=value.st_ctime_ns)
        return value

    monkeypatch.setattr(helper.os, "fstat", changed)
    with pytest.raises(ValueError, match="source changed"):
        helper.snapshot(body)
    assert not (Path(body["root"]) / body["key"] / "manifest.json").exists()
