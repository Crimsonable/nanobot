import json
import subprocess
from pathlib import Path

from channel_gateway.legacy_events import translate


def event(kind, **data):
    return {"id": "evt_" + kind, "type": kind, "created": 2000,
            "data": {"sessionID": "ses_test", **data}}


def test_projection_produces_valid_v1_messages_and_parts_without_mutating_native():
    state, frames = {"directory": "/workspace"}, []
    step = {"assistantMessageID": "msg_answer"}
    tool = {**step, "id": "call_read"}
    tokens = {"input": 12, "output": 4, "reasoning": 1, "cache": {"read": 2, "write": 0}}
    native = [
        event("session.inbox.enqueued", inboxID="msg_input",
              item={"type": "user", "payload": {"text": "read"}, "delivery": "queue"}),
        event("session.inbox.delivered", inboxID="msg_input"),
        event("session.step.started", **step, agent="build", started=1000, model={"providerID": "test", "id": "model"}),
        event("session.text.started", **step, ordinal=0),
        event("session.text.delta", **step, ordinal=0, delta="one"),
        event("session.text.ended", **step, ordinal=0, text="one two"),
        event("session.reasoning.started", **step, ordinal=1),
        event("session.reasoning.ended", **step, ordinal=1, text="thinking"),
        event("session.tool.input.started", **tool, name="read"),
        event("session.tool.input.delta", **tool, delta='{"path":'),
        event("session.tool.input.ended", **tool, text='{"path":"file.txt"}'),
        event("session.tool.called", **tool, input={"path": "file.txt"}, executed=True),
        event("session.tool.progress", **tool, metadata={"title": "reading"}),
        event("session.tool.success", **tool, executed=True, content=[{"type": "text", "text": "file content"},
            {"type": "file", "uri": "data:image/png;base64,aQ==", "mime": "image/png", "name": "preview.png"}]),
        event("session.step.ended", **step, finish="stop", cost=0, tokens=tokens),
        event("session.execution.succeeded"),
    ]
    original = json.dumps(native)
    for item in native:
        frames.extend(translate(item, state))
        # Persistence/restart must preserve the projection, including partial tool input.
        state = json.loads(json.dumps(state))
    assert json.dumps(native) == original
    infos = [frame["properties"]["info"] for frame in frames if frame["type"] == "message.updated"]
    assert infos[0]["role"] == "user" and infos[0]["model"]["modelID"] == "model"
    assert infos[-1]["parentID"] == "msg_input" and infos[-1]["tokens"] == tokens
    parts = [frame["properties"]["part"] for frame in frames if frame["type"] == "message.part.updated"]
    completed = next(part for part in parts if part.get("state", {}).get("status") == "completed")
    assert completed["state"]["input"] == {"filePath": "file.txt"}
    assert completed["state"]["attachments"][0]["filename"] == "preview.png"
    text = [part for part in parts if part["messageID"] == "msg_answer" and part["type"] == "text"]
    assert text[0]["text"] == "" and text[-1]["text"] == "one two"
    assert text[0]["id"] == text[-1]["id"]
    # The pinned SDK includes the V1 schema, used only by this boundary contract test.
    script = """
      import { Schema } from 'effect';
      import * as V1 from '@opencode/schema/session-v1';
      let data=''; for await (const chunk of process.stdin) data+=chunk;
      for (const frame of JSON.parse(data)) {
        if (frame.type === 'message.updated') Schema.decodeUnknownSync(V1.Info)(frame.properties.info);
        if (frame.type === 'message.part.updated') Schema.decodeUnknownSync(V1.Part)(frame.properties.part);
      }
    """
    result = subprocess.run(["node", "--input-type=module", "-e", script], input=json.dumps(frames),
        text=True, capture_output=True, cwd=Path(__file__).resolve().parents[2] / "opencode_plugin")
    assert result.returncode == 0, result.stderr


def test_unknown_events_stay_native_and_failures_map_to_legacy_errors():
    assert translate(event("future.native", opaque={"x": 1}), {}) == []
    frames = translate(event("session.execution.failed", error={"type": "provider", "message": "quota"}), {})
    assert frames[0]["type"] == "session.error"
    assert frames[0]["properties"]["error"] == {"name": "UnknownError", "data": {"message": "quota"}}
    assert [frame["type"] for frame in frames[-2:]] == ["session.status", "session.idle"]


def test_permission_and_compaction_projection():
    frame, = translate(event("permission.asked", id="per_test", action="edit", resources=["file"],
        source={"type": "tool", "messageID": "msg_a", "id": "call_a"}), {})
    assert frame["properties"]["permission"] == "edit"
    assert frame["properties"]["tool"] == {"messageID": "msg_a", "callID": "call_a"}
    frame, = translate(event("session.compaction.ended", text="internal summary"), {})
    assert frame["type"] == "session.compacted"
    assert "internal summary" not in json.dumps(frame)


def test_question_forms_project_answers_and_cancellation():
    state = {}
    created = event("form.created", form={"id": "frm_test", "sessionID": "ses_test",
        "metadata": {"kind": "question", "tool": {"messageID": "msg_a", "id": "call_a"}},
        "fields": [{"key": "q0", "type": "string", "title": "Choice", "description": "Which?",
                    "options": [{"value": "A", "label": "A", "description": "First"}]}]})
    frame, = translate(created, state)
    assert frame["type"] == "question.asked"
    assert frame["properties"]["questions"][0]["question"] == "Which?"
    assert frame["properties"]["tool"] == {"messageID": "msg_a", "callID": "call_a"}
    frame, = translate(event("form.replied", id="frm_test", answer={"q0": "A"}), state)
    assert frame["properties"] == {"requestID": "frm_test", "sessionID": "ses_test", "answers": [["A"]]}
    translate(created, state)
    frame, = translate(event("form.cancelled", id="frm_test"), state)
    assert frame["type"] == "question.rejected"
    created["data"]["form"]["metadata"]["kind"] = "credential"
    assert translate(created, {}) == []
