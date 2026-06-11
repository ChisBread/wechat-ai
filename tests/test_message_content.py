from types import SimpleNamespace
from unittest.mock import patch

import pytest

from wechat_ai_bot.weixin.message_content import message_content_text


def _message_classes():
    pytest.importorskip("google.protobuf")
    pytest.importorskip("xmltodict")
    from wechat_ai_bot.models import UserInfo
    from wechat_ai_bot.weixin.message_classes import MessageType, TextMessage

    return UserInfo, MessageType, TextMessage


def _text_message(content, *, source="", contact=None, room=None):
    UserInfo, MessageType, TextMessage = _message_classes()
    return TextMessage(
        local_id=1,
        server_id=2,
        local_type=MessageType.Text,
        sort_seq=3,
        real_sender_id=4,
        create_time=1780998120,
        status=0,
        upload_status=0,
        download_status=0,
        server_seq=0,
        origin_source=0,
        source=source,
        message_content=content,
        compress_content=None,
        packed_info_data=None,
        message_db_path="/tmp/message_0.db",
        contact=contact,
        room=room,
        user_info=UserInfo(account="me"),
        content="",
    )


def test_message_content_text_decodes_plain_bytes():
    assert message_content_text("你好") == "你好"
    assert message_content_text("你好".encode("utf-8")) == "你好"


def test_message_content_text_decompresses_ct4_zstd_bytes():
    zstd = pytest.importorskip("zstandard")
    raw = zstd.ZstdCompressor().compress("面试官：你好".encode("utf-8"))

    assert message_content_text(raw, 4) == "面试官：你好"


def test_message_content_text_detects_zstd_magic_without_ct_flag():
    zstd = pytest.importorskip("zstandard")
    raw = zstd.ZstdCompressor().compress("自动识别压缩消息".encode("utf-8"))

    assert message_content_text(raw) == "自动识别压缩消息"


def test_message_content_text_never_formats_python_bytes_repr():
    with patch(
        "wechat_ai_bot.weixin.message_content.decompress_zstd_text",
        return_value="压缩文本",
    ):
        text = message_content_text(b"\x28\xb5\x2f\xfdcompressed", 4)

    assert text == "压缩文本"
    assert "b'" not in text


def test_text_message_parsed_content_decodes_plain_bytes():
    msg = _text_message("普通文本".encode("utf-8"), source="<atuserlist>me</atuserlist>".encode("utf-8"))

    assert msg.parsed_content == "普通文本"
    assert msg.parsed_source == "<atuserlist>me</atuserlist>"


def test_text_message_parsed_content_decodes_zstd_bytes():
    with patch(
        "wechat_ai_bot.weixin.message_content.decompress_zstd_text",
        return_value="压缩后的文本",
    ):
        msg = _text_message(b"\x28\xb5\x2f\xfdcompressed")

        assert msg.parsed_content == "压缩后的文本"


def test_text_message_parsed_content_keeps_group_prefix_stripping():
    contact = SimpleNamespace(username="alice", display_name="Alice")
    room = SimpleNamespace(username="room@chatroom", display_name="Room")
    msg = _text_message("alice:\n群聊文本".encode("utf-8"), contact=contact, room=room)

    assert msg.parsed_content == "群聊文本"
