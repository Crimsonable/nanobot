from channel_gateway.attachments import attachment_from_content_url
from channel_gateway.feishu_im_tool import FeishuIMParser


def test_reference_from_content_url():
    assert attachment_from_content_url("https://files.example/report.pdf", "https://files.example/") == {
        "url": "https://files.example/report.pdf", "source": "content_url"}


def test_feishu_private_resource_remains_reference():
    parser = FeishuIMParser(frontend_id="feishu", frontend_config={"app_id": "test", "app_secret": "test"})
    ref = parser._resource_reference(user_id="alice", attachment_group="chat", message_id="message",
        file_key="file", resource_type="file", fallback_filename="report.pdf")
    assert ref["url"].endswith("/messages/message/resources/file?type=file")
    assert ref["filename"] == "report.pdf"
    assert ref["requires_auth"] is True
