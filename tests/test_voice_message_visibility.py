"""Regression coverage for reads racing with native voice transcription."""
import json
from queue import Queue
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from tests.test_debug_routes import (
    DummyConfig, DummyMediaBot, dashboard_auth_env, dashboard_auth_headers,
)
from tests.test_voice_transcription import voice
from wechat_ai_bot.mcp.app import create_app
from wechat_ai_bot.mcp.message_visibility import get_display_messages
from wechat_ai_bot.models import UserInfo
from wechat_ai_bot.services.core.voice_transcription_service import VoiceTranscriptionService
from wechat_ai_bot.weixin.parser.util.protocbuf.packed_info_data_pb2 import PackedInfoData


@pytest.mark.parametrize("chat", ["alice", "room@chatroom"])
def test_all_lists_wait_for_database_transcript_even_before_enqueue_and_after_timeout(tmp_path, chat):
    bot = DummyMediaBot(tmp_path)
    db = bot.database_service
    db._message_username_map = {chat: set()}
    incoming = list(db._image_row())
    incoming[2], incoming[12], incoming[14] = 34, "<msg/>", b""
    older = db._text_row()
    db.get_messages_by_username = lambda *a, **kw: [tuple(incoming), older][:kw.get("count", 10)]
    queue = Queue()
    bot.voice_transcription_service = VoiceTranscriptionService(queue, enabled=True)
    app = create_app(UserInfo(account="me"), DummyConfig({"debug": {"enabled": True}}), bot=bot)

    def tool(name, *args):
        result = app._tool_manager._tools[name].fn(None, *args)
        return json.loads(result if isinstance(result, str) else result[0].text)

    with dashboard_auth_env(), TestClient(app.streamable_http_app()) as client:
        def check(ready):
            expected = ["1001", "1002"] if ready else ["1001"]
            for parse_media in (False, True):
                messages = tool("get_recent_messages", chat, 10, True, parse_media)
                assert [m["server_id"] for m in messages["messages"]] == expected
                assert messages["count"] == len(expected)
                response = client.get(
                    f"/dashboard/api/messages/recent?username={chat}&parse_media={str(parse_media).lower()}",
                    headers=dashboard_auth_headers(),
                )
                assert response.status_code == 200
                assert [m["server_id"] for m in response.json()["messages"]] == expected
                if ready:
                    assert response.json()["messages"][-1]["text"] == "【语音】识别完成"
            media = tool("get_recent_media_messages", chat, 10)
            assert [m["server_id"] for m in media["messages"]] == (["1002"] if ready else [])
            media = client.get(f"/dashboard/api/messages/media?username={chat}", headers=dashboard_auth_headers()).json()
            assert [m["server_id"] for m in media["messages"]] == (['1002', '1001'] if ready else ['1001'])
            summary = tool("get_chat_summary_context", chat, 10)
            assert summary["count"] == len(expected)
            assert ("识别完成" in summary["context"]) == ready
            preview = "1002" if ready else "1001"
            assert tool("get_recent_chats", 10)["chats"][0]["last_message"]["server_id"] == preview
            chats = client.get("/dashboard/api/chats", headers=dashboard_auth_headers()).json()
            assert chats["chats"][0]["last_message"]["server_id"] == preview

        # A DB read can arrive before the listener has registered a pending job.
        check(False)
        message = voice(is_chatroom=chat.endswith("@chatroom"),
                        room=SimpleNamespace(username=chat, display_name="Room"))
        assert bot.voice_transcription_service.submit(message, lambda _: None)
        check(False)
        request = queue.get_nowait()
        request.execution_result.update(verified=False, error="timeout")
        request.on_complete()
        check(False)
        retry = queue.get_nowait()
        retry.execution_result.update(verified=False, error="timeout")
        retry.on_complete()
        assert bot.voice_transcription_service.get_status()["pending"] == 0
        check(False)
        # Late persistence or a manual retry should make the same ID appear once.
        packed = PackedInfoData()
        packed.info.audioTxt = "识别完成"
        incoming[14] = packed.SerializeToString()
        check(True)
        check(True)


@pytest.mark.parametrize("enabled,upload_status,sender,xml,visible", [
    (False, 0, "alice", "<msg/>", True),
    (True, 0, "alice", "<msg/>", False),
    (True, 1, "alice", "<msg/>", True),
    (True, 0, "me", "<msg/>", True),
    (True, 0, "alice", '<msg><voicetrans transtext="文字"/></msg>', True),
    (True, 0, "alice", '<msg><voicetrans transtext="   "/></msg>', False),
])
def test_visibility_respects_setting_direction_and_xml(tmp_path, enabled, upload_status, sender, xml, visible):
    bot = DummyMediaBot(tmp_path)
    db = bot.database_service
    row = list(db._image_row())
    row[2], row[7], row[12] = 34, upload_status, xml
    db.get_messages_by_username = lambda *a, **kw: [tuple(row)]
    db.get_contact_by_sender_id = lambda *a: SimpleNamespace(username=sender)
    bot.voice_transcription_service = SimpleNamespace(enabled=enabled)
    assert bool(get_display_messages(db, "alice", count=1, bot=bot)) == visible
