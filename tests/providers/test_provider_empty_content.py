from nanobot.providers.base import LLMProvider


def test_sanitize_user_missing_content_uses_empty_fallback():
    messages = [{"role": "user"}]

    assert LLMProvider._sanitize_empty_content(messages) == [
        {"role": "user", "content": "(empty)"}
    ]


def test_sanitize_user_none_content_uses_empty_fallback():
    messages = [{"role": "user", "content": None}]

    assert LLMProvider._sanitize_empty_content(messages) == [
        {"role": "user", "content": "(empty)"}
    ]


def test_sanitize_user_blank_string_uses_empty_fallback():
    messages = [{"role": "user", "content": "   \n\t"}]

    assert LLMProvider._sanitize_empty_content(messages) == [
        {"role": "user", "content": "(empty)"}
    ]


def test_sanitize_user_quoted_empty_string_uses_empty_fallback():
    messages = [{"role": "user", "content": '""'}]

    assert LLMProvider._sanitize_empty_content(messages) == [
        {"role": "user", "content": "(empty)"}
    ]


def test_sanitize_user_empty_list_uses_empty_fallback():
    messages = [{"role": "user", "content": []}]

    assert LLMProvider._sanitize_empty_content(messages) == [
        {"role": "user", "content": "(empty)"}
    ]


def test_sanitize_user_empty_text_block_uses_empty_fallback():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": ""},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}, "_meta": {"path": "x.png"}},
        {"type": "input_text", "text": "   "},
        {"type": "output_text", "text": '""'},
    ]}]

    assert LLMProvider._sanitize_empty_content(messages) == [
        {"role": "user", "content": [
            {"type": "text", "text": "(empty)"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            {"type": "input_text", "text": "(empty)"},
            {"type": "output_text", "text": "(empty)"},
        ]}
    ]


def test_sanitize_assistant_tool_call_empty_content_stays_none():
    messages = [{"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]}]

    assert LLMProvider._sanitize_empty_content(messages) == [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}]}
    ]
