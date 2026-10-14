import pytest

@pytest.fixture(autouse=True)
def frontend_content(tmp_path, request):
    if request.module.__name__.split(".")[-1] not in {"test_runtime", "test_session_directories", "test_supervisor_assets", "test_plugin_events", "test_frontend_workspaces"}:
        return
    for frontend in ["web", "web-a", "web-b"]:
        (tmp_path / "common" / frontend / "app").mkdir(parents=True, exist_ok=True)
