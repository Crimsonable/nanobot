from container_up.bridge_protocol import (
    PROTOCOL_VERSION,
    build_register_packet,
    parse_register_packet,
)


def test_build_register_packet_includes_required_fields() -> None:
    packet = build_register_packet(
        org_id="org-a",
        container_name="nanobot-org-a",
        token="secret",
    )

    assert packet == {
        "type": "register",
        "version": PROTOCOL_VERSION,
        "org_id": "org-a",
        "container_name": "nanobot-org-a",
        "token": "secret",
    }


def test_parse_register_packet_requires_org_and_container_name() -> None:
    org_id, container_name = parse_register_packet(
        {
            "type": "register",
            "org_id": "org-a",
            "container_name": "nanobot-org-a",
        }
    )

    assert org_id == "org-a"
    assert container_name == "nanobot-org-a"


def test_parse_register_packet_rejects_missing_required_fields() -> None:
    try:
        parse_register_packet({"type": "register", "org_id": "org-a"})
    except ValueError as exc:
        assert str(exc) == "missing container_name"
    else:
        raise AssertionError("expected ValueError for missing container_name")
