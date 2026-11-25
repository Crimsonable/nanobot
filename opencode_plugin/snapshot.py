"""Local publication helper invoked by the trusted plugin; no daemon or remote file access."""
import fcntl
import hashlib
import json
import mimetypes
import os
import shutil
import stat
import sys
import time
from pathlib import Path


def open_workspace_file(workspace, source):
    """Resolve every component via directory FDs; ancestor symlink swaps cannot escape."""
    parts = source.relative_to(workspace).parts
    parent = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)


def snapshot(body):
    scope = body["scope"]
    root = Path(body["root"])
    root.mkdir(parents=True, exist_ok=True)
    directory = root / body["key"]
    workspace = Path(scope["workspace"]).resolve()
    with Path(scope["lock_path"]).open("a") as session_lock:
        fcntl.flock(session_lock, fcntl.LOCK_EX)
        with (root / ".quota.lock").open("a") as quota_lock:
            fcntl.flock(quota_lock, fcntl.LOCK_EX)
            if time.time() >= scope["deadline_at"]:
                raise ValueError("request deadline expired")
            for previous in root.iterdir():
                if previous.is_dir() and (not (previous / "manifest.json").exists()
                        or float((previous / "expires").read_text()) <= time.time()):
                    # The quota lock excludes other snapshot writers. A directory
                    # without a committed manifest is an interrupted copy.
                    shutil.rmtree(previous)
            manifest = directory / "manifest.json"
            if manifest.exists():
                return json.loads(manifest.read_text())
            directory.mkdir(exist_ok=True)
            (directory / "expires").write_text(str(scope["deadline_at"]))
            used = 0
            for saved in root.glob("*/*.data"):
                try:
                    used += saved.stat().st_size
                except FileNotFoundError:
                    pass  # A completed upload can delete its snapshot concurrently.
            output = []
            for index, item in enumerate(body["files"]):
                source = Path(os.path.abspath(workspace / item["path"]))
                if not source.is_relative_to(workspace):
                    raise PermissionError("path outside user workspace")
                if any(p.is_symlink() for p in [source, *source.parents]):
                    raise PermissionError("symbolic links are not accepted")
                filename = item.get("filename") or source.name
                if not filename or filename in {".", ".."} or any(c in filename for c in "/\\\x00\r\n"):
                    raise ValueError("invalid filename")
                fd = open_workspace_file(workspace, source)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise PermissionError("only regular singly-linked files can be published")
                    if info.st_size > scope["file_bytes"] or used + info.st_size > scope["spool_bytes"]:
                        raise ValueError("publication size limit exceeded")
                    target = directory / f"{index}.data"
                    count, checksum = 0, hashlib.sha256()
                    with target.open("wb") as destination:
                        while chunk := stream.read(256 * 1024):
                            count += len(chunk)
                            if count > scope["file_bytes"] or used + count > scope["spool_bytes"]:
                                raise ValueError("publication size limit exceeded")
                            destination.write(chunk)
                            checksum.update(chunk)
                        destination.flush()
                        os.fsync(destination.fileno())
                    final = os.fstat(stream.fileno())
                    if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (final.st_size, final.st_mtime_ns, final.st_ctime_ns):
                        raise ValueError("source changed during publication")
                    used += count
                output.append({"path": str(target), "filename": filename, "size": count,
                    "sha256": checksum.hexdigest(), "content_type": mimetypes.guess_type(filename)[0]
                    or "application/octet-stream", "attachment_id": "att_" + hashlib.sha256(
                        f"{body['key']}:{index}".encode()).hexdigest()})
            temporary = manifest.with_suffix(".tmp")
            with temporary.open("w") as stream:
                json.dump(output, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(manifest)
            return output


if __name__ == "__main__":
    body = json.load(sys.stdin)
    print(json.dumps(snapshot(body)))
