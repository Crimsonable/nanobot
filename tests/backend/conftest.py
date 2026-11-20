import pytest


@pytest.fixture(autouse=True)
def frontend_content(tmp_path, request):
    if request.module.__name__.split(".")[-1] not in {"test_adapter", "test_session_directories", "test_opencode_assets", "test_native_events", "test_frontend_workspaces", "test_plugin_sandbox_linux"}:
        return
    for frontend in ["web", "web-a", "web-b"]:
        (tmp_path / "common" / frontend / "app").mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def publication_lock_on_windows(monkeypatch):
    import sys
    if sys.platform == "win32":
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def lock(_):
            yield
        monkeypatch.setattr("bucket_runtime.adapter.publication_lock", lock)
