from channel_gateway.native_reply import accumulate, result_for


def resolve(events, **result):
    state = {}
    for kind, data in events:
        accumulate(state, {"type": kind, "data": data})
    state["execution"] = "session.execution.succeeded"
    return result_for({"request_id": "r", "status": "completed", **result}, state, [])


def test_final_step_text_is_ordered_and_usage_spans_all_steps():
    tokens = {"input": 10, "output": 3, "reasoning": 2, "cache": {"read": 4, "write": 1}}
    events = [
        ("session.step.started", {"assistantMessageID": "tool"}),
        ("session.text.ended", {"assistantMessageID": "tool", "ordinal": 0, "text": "working"}),
        (
            "session.step.ended",
            {"assistantMessageID": "tool", "finish": "tool-calls", "tokens": tokens},
        ),
        ("session.step.started", {"assistantMessageID": "final"}),
        (
            "session.reasoning.ended",
            {"assistantMessageID": "final", "ordinal": 0, "text": "private"},
        ),
        ("session.text.ended", {"assistantMessageID": "final", "ordinal": 2, "text": "second"}),
        ("session.text.ended", {"assistantMessageID": "final", "ordinal": 1, "text": "first"}),
        ("session.compaction.ended", {"text": "internal summary"}),
        ("session.step.ended", {"assistantMessageID": "final", "finish": "stop", "tokens": tokens}),
    ]
    result = resolve(events)
    assert result.content == "first\nsecond" and result.status == "completed"
    assert result.usage.model_dump() == {
        "input_tokens": 20,
        "output_tokens": 6,
        "reasoning_tokens": 4,
        "cache_read_tokens": 8,
        "cache_write_tokens": 2,
    }


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
