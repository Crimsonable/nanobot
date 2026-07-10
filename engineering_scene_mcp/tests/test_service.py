from engineering_scene_mcp.service import build_chat_completions_url


def test_build_chat_completions_url_appends_endpoint() -> None:
    assert build_chat_completions_url("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000/v1/chat/completions"


def test_build_chat_completions_url_handles_trailing_slash() -> None:
    assert build_chat_completions_url("http://127.0.0.1:8000/v1/") == "http://127.0.0.1:8000/v1/chat/completions"
