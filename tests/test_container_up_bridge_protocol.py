from container_up.bridge_protocol import (
    PROTOCOL_VERSION,
    build_register_packet,
    is_terminal_event,
    make_pending_key,
    make_session_key,
    parse_register_packet,
)


def test_build_register_packet_includes_required_fields() -> None:
    packet = build_register_packet(
        session_id="demo-session",
        container_name="nanobot-session-demo",
        token="secret",
    )

    assert packet == {
        "type": "register",
        "version": PROTOCOL_VERSION,
        "session_id": "demo-session",
        "container_name": "nanobot-session-demo",
        "token": "secret",
    }


def test_parse_register_packet_requires_session_and_container_name() -> None:
    session_id, container_name = parse_register_packet(
        {
            "type": "register",
            "session_id": "demo-session",
            "container_name": "nanobot-session-demo",
        }
    )

    assert session_id == "demo-session"
    assert container_name == "nanobot-session-demo"


def test_parse_register_packet_rejects_missing_required_fields() -> None:
    try:
        parse_register_packet({"type": "register", "session_id": "demo-session"})
    except ValueError as exc:
        assert str(exc) == "missing container_name"
    else:
        raise AssertionError("expected ValueError for missing container_name")


def test_make_pending_key_is_session_scoped() -> None:
    assert make_pending_key("session-a", "req-1") != make_pending_key("session-b", "req-1")


def test_make_session_key_matches_existing_remote_format() -> None:
    assert make_session_key("tenant-a", "conv-1") == "remote:tenant-a:conv-1"


def test_is_terminal_event_matches_bridge_lifecycle() -> None:
    assert is_terminal_event({"type": "final"}) is True
    assert is_terminal_event({"type": "error"}) is True
    assert is_terminal_event({"type": "cancelled"}) is True
    assert is_terminal_event({"type": "progress"}) is False
