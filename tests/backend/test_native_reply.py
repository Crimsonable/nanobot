from types import SimpleNamespace

from agent_backend.models import Result
from channel_gateway.native_reply import resolve_reply


def resolve(events, **result):
    records = [{"bucket_id": "b", "request_id": "r", "payload": {
        "session_id": "s", "event": {"type": kind, "data": data}}} for kind, data in events]
    # A child and another request must not contaminate the root answer or usage.
    records.extend([{**row, "request_id": "other"} for row in records])
    records.extend([{**row, "payload": {**row["payload"], "session_id": "child"}} for row in records[:2]])
    return resolve_reply(SimpleNamespace(records=lambda _: records), "b", {"session_id": "s"},
                         Result(request_id="r", status="completed", **result))


def test_final_step_text_is_ordered_and_usage_spans_all_steps():
    tokens = {"input": 10, "output": 3, "reasoning": 2, "cache": {"read": 4, "write": 1}}
    events = [
        ("session.step.started", {"assistantMessageID": "tool"}),
        ("session.text.ended", {"assistantMessageID": "tool", "ordinal": 0, "text": "working"}),
        ("session.step.ended", {"assistantMessageID": "tool", "finish": "tool-calls", "tokens": tokens}),
        ("session.step.started", {"assistantMessageID": "final"}),
        ("session.reasoning.ended", {"assistantMessageID": "final", "ordinal": 0, "text": "private"}),
        ("session.text.ended", {"assistantMessageID": "final", "ordinal": 2, "text": "second"}),
        ("session.text.ended", {"assistantMessageID": "final", "ordinal": 1, "text": "first"}),
        ("session.compaction.ended", {"text": "internal summary"}),
        ("session.step.ended", {"assistantMessageID": "final", "finish": "stop", "tokens": tokens}),
    ]
    result = resolve(events)
    assert result.content == "first\nsecond" and result.status == "completed"
    assert result.usage.model_dump() == {"input_tokens": 20, "output_tokens": 6, "reasoning_tokens": 4,
                                         "cache_read_tokens": 8, "cache_write_tokens": 2}


def test_incomplete_latest_step_never_returns_earlier_answer():
    events = [
        ("session.step.started", {"assistantMessageID": "old"}),
        ("session.text.ended", {"assistantMessageID": "old", "ordinal": 0, "text": "stale"}),
        ("session.step.ended", {"assistantMessageID": "old", "finish": "stop"}),
        ("session.step.started", {"assistantMessageID": "new"}),
        ("session.text.ended", {"assistantMessageID": "new", "ordinal": 0, "text": "partial"}),
    ]
    result = resolve(events)
    assert result.status == "failed" and result.error == "NATIVE_REPLY_INCOMPLETE"
    assert result.content == ""
    assert resolve(events, error="REQUEST_TIMEOUT").error == "REQUEST_TIMEOUT"
