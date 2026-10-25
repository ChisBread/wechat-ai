from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock
import logging

import pytest

from wechat_ai_bot.rpa.action_handlers.transcribe_voice_handler import (
    TranscribeVoiceAction, TranscribeVoiceHandler,
)
from wechat_ai_bot.services.core.voice_transcription_service import VoiceTranscriptionService
from wechat_ai_bot.services.core.processor_service import ProcessorService
from wechat_ai_bot.weixin.parser.audio_parser import audio_transcript
from wechat_ai_bot.weixin.parser.util.protocbuf.packed_info_data_pb2 import PackedInfoData
from wechat_ai_bot.mcp.dispatchers import QueueCommandDispatcher


def voice(**kwargs):
    fields = dict(local_type=34, is_self=False, audio_text="", is_chatroom=False,
                  room=None, contact=SimpleNamespace(username="alice", display_name="Alice"),
                  user_info=SimpleNamespace(account="me"), server_id="123",
                  message_db_path="/tmp/message.db")
    return SimpleNamespace(**(fields | kwargs))


def test_reads_native_packed_field_and_xml_priority():
    packed = PackedInfoData()
    packed.info.audioTxt = "测试测试。"
    assert audio_transcript("<msg/>", packed.SerializeToString()) == "测试测试。"
    assert audio_transcript('<msg><voicetrans transtext="已有文字"/></msg>', packed.SerializeToString()) == "已有文字"
    assert audio_transcript("bad xml", b"\xff") == ""


@pytest.mark.parametrize("fields", [{"local_type": 1}, {"is_self": True},
                                      {"audio_text": "existing"}, {"contact": None}])
def test_ineligible_messages_do_not_touch_ui(fields):
    queue = Queue()
    service = VoiceTranscriptionService(queue, enabled=True)
    assert not service.submit(voice(**fields), Mock())
    assert queue.empty()


def test_disabled_by_default():
    assert not VoiceTranscriptionService(Queue()).submit(voice(), Mock())


@pytest.mark.parametrize("success", [True, False])
def test_completion_delivers_once_and_duplicate_notifications_are_ignored(success):
    queue, deliver = Queue(), Mock()
    service = VoiceTranscriptionService(queue, enabled=True)
    message = voice()
    assert service.submit(message, deliver)
    assert service.submit(voice(), deliver)
    action = queue.get_nowait()
    assert queue.empty()
    assert not action.is_send_message
    if success:
        action.execution_result.update(verified=True, voice_to_text="识别结果")
    action.on_complete()
    action.on_complete()
    if not success:
        deliver.assert_not_called()
        assert service.submit(voice(), deliver)
        retry = queue.get_nowait()
        assert retry is not action
        retry.on_complete()
        retry.on_complete()
        assert service.get_status()["retried"] == 1
        assert service.get_status()["failed"] == 1
    deliver.assert_called_once_with(message)
    assert message.audio_text == ("识别结果" if success else "")
    assert service.submit(voice(), deliver)
    assert queue.empty()


def test_group_uses_room_identity_and_queue_backpressure_preserves_original():
    queue = Queue(maxsize=1)
    service = VoiceTranscriptionService(queue, enabled=True)
    room = SimpleNamespace(username="room@chatroom", display_name="Room")
    assert service.submit(voice(is_chatroom=True, room=room), Mock())
    assert queue.queue[0].target_username == "room@chatroom"
    assert not service.submit(voice(server_id="456"), Mock())
    assert service.get_status()["pending"] == 1


def handler():
    item = TranscribeVoiceHandler.__new__(TranscribeVoiceHandler)
    item.logger = logging.getLogger("test")
    item.controller = SimpleNamespace(db=Mock())
    item.window_manager = Mock()
    item._cleanup = Mock()
    return item


def action():
    return TranscribeVoiceAction(target="Alice", target_username="alice", server_id="123",
                                 message_db_path="/tmp/message.db", timeout=1)


def test_exact_database_identity_and_existing_text_skips_ui():
    item = handler()
    row = [None] * 18
    row[2], row[12], row[14], row[15] = 34, "<msg/>", b"", 0
    packed = PackedInfoData()
    packed.info.audioTxt = "数据库结果"
    row[14] = packed.SerializeToString()
    item.controller.db.get_message_by_server_id.return_value = row
    request = action()
    assert item.execute(request)
    assert request.execution_result["voice_to_text"] == "数据库结果"
    args = item.controller.db.get_message_by_server_id.call_args.args
    assert str(args[1]) == "/tmp/message.db" and args[0] == "123" and args[2] == "alice"
    assert item.controller.db.get_message_by_server_id.call_args.kwargs == {"fresh": True}
    item.window_manager.switch_session.assert_not_called()


def test_wrong_conversation_never_clicks_voice():
    item = handler()
    item._read = Mock(return_value="")
    item._title_matches = Mock(return_value=False)
    item._incoming_bubbles = Mock()
    request = action()
    assert not item.execute(request)
    assert not request.execution_result["verified"]
    item._incoming_bubbles.assert_not_called()
    item._cleanup.assert_called_once()


def test_click_does_not_imply_transcription_success(monkeypatch):
    import wechat_ai_bot.rpa.action_handlers.transcribe_voice_handler as module
    item = handler()
    item._read = Mock(return_value="")
    item._title_matches = Mock(return_value=True)
    item._incoming_bubbles = Mock(side_effect=[[[10, 20, 80, 50]], []])
    item.window_manager.get_message_region.return_value = [100, 80, 500, 900]
    item._click_transcribe_menu = Mock(return_value=True)
    item._flush_client_transcript = Mock()
    monkeypatch.setattr(module, "mouse", Mock())
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    ticks = iter([0, 0, 0, 2, 2])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    request = action()
    assert not item.execute(request)
    assert request.execution_result["triggered"]
    assert not request.execution_result["verified"]
    item._click_transcribe_menu.assert_called_once()


@pytest.mark.parametrize("label, expected", [("语音转文字", True), ("转文字", True),
                                            ("图语音转文字", True), ("▢语音转文字", True),
                                            ("图标语音转文字", True), ("语音转文", False),
                                            ("abc语音转文字", False), ("语音转文字中", False),
                                            ("收起文字", False), ("删除", False)])
@pytest.mark.parametrize("anchor", ["收藏", "图收藏"])
def test_menu_only_clicks_complete_transcription_labels(monkeypatch, label, expected, anchor):
    import wechat_ai_bot.rpa.action_handlers.transcribe_voice_handler as module
    item = handler()
    item.image_processor = Mock()
    item.ocr_processor = Mock()
    item.ocr_processor.process_image.return_value = [
        {"label": anchor, "pixel_bbox": [10, 80, 80, 100]},
        {"label": label, "pixel_bbox": [10, 20, 110, 40]},
    ]
    mouse = Mock()
    mouse.position.return_value = (400, 1600)
    mouse.size.return_value = SimpleNamespace(width=1920, height=1916)
    monkeypatch.setattr(module, "mouse", mouse)
    assert item._click_transcribe_menu() == expected
    assert mouse.click.call_count == int(expected)


@pytest.mark.parametrize("prefix", ["", "图"])
def test_menu_prefers_popup_over_inline_hint(monkeypatch, prefix):
    import wechat_ai_bot.rpa.action_handlers.transcribe_voice_handler as module
    item = handler()
    item.image_processor = Mock()
    item.ocr_processor = Mock()
    item.ocr_processor.process_image.return_value = [
        {"label": "转文字", "pixel_bbox": [50, 5, 100, 20]},
        {"label": prefix + "语音转文字", "pixel_bbox": [10, 30, 110, 50]},
        {"label": prefix + "收藏", "pixel_bbox": [10, 80, 60, 100]},
    ]
    mouse = Mock()
    mouse.position.return_value = (400, 1600)
    mouse.size.return_value = SimpleNamespace(width=1920, height=1916)
    monkeypatch.setattr(module, "mouse", mouse)
    assert item._click_transcribe_menu()
    mouse.click.assert_called_once_with(240, 1260)


def test_processor_defers_plugin_delivery_until_transcription_finishes():
    processor = ProcessorService.__new__(ProcessorService)
    processor.logger = logging.getLogger("test")
    message = voice()
    processor.message_factory_service = SimpleNamespace(create_message=lambda _: message)
    processor.async_runner = Mock()
    processor.db = None
    processor.user_info = message.user_info
    queue = Queue()
    processor.voice_transcription = VoiceTranscriptionService(queue, enabled=True)
    processor._process_message(object())
    processor.async_runner.submit_task.assert_not_called()
    request = queue.get_nowait()
    request.execution_result.update(verified=True, voice_to_text="新的语音")
    request.on_complete()
    processor.async_runner.submit_task.assert_called_once()
    assert processor.async_runner.submit_task.call_args.args[0].audio_text == "新的语音"


def test_dispatcher_queues_transcription_without_send_semantics():
    bot = SimpleNamespace(rpa_task_queue=Queue())
    QueueCommandDispatcher(bot, None).dispatch_rpa("transcribe_voice", {
        "target": "Alice", "target_username": "alice", "server_id": "123",
        "message_db_path": "/tmp/message.db",
    })
    request = bot.rpa_task_queue.get_nowait()
    assert request.server_id == "123" and not request.is_send_message


def test_mcp_plain_history_includes_native_transcript(tmp_path):
    import asyncio
    import json
    from wechat_ai_bot.mcp.app import create_app
    from wechat_ai_bot.models import UserInfo
    from tests.test_debug_routes import DummyConfig, DummyMediaBot

    bot = DummyMediaBot(tmp_path)
    row = list(bot.database_service._image_row())
    packed = PackedInfoData()
    packed.info.audioTxt = "语音内容"
    row[2], row[12], row[14] = 34, "<msg/>", packed.SerializeToString()
    bot.database_service.get_messages_by_username = lambda *a, **kw: [tuple(row)]
    app = create_app(UserInfo(account="me"), DummyConfig({}), bot=bot)
    result = asyncio.run(app.call_tool("get_recent_messages", {"contact_name": "media"}))
    payload = json.loads(result[0].text)
    assert payload["messages"][0]["voice_to_text"] == "语音内容"
    assert payload["messages"][0]["text"] == "【语音】语音内容"


def test_fresh_read_reopens_only_target_database_without_resetting_sequences():
    from pathlib import Path
    from threading import RLock
    from wechat_ai_bot.services.core.linux_database_service import LinuxDatabaseService
    db = LinuxDatabaseService.__new__(LinuxDatabaseService)
    db._lock = RLock()
    db._table_for_username = Mock(return_value="Msg_test")
    db._drop_connection = Mock()
    db.execute_query = Mock(return_value=[("updated",)])
    db._normalize_message_row = Mock(side_effect=lambda row, path: row)
    db._table_sequences = {("existing", "Msg_test"): 148}
    assert db.get_message_by_server_id("123", Path("/tmp/message.db"), "alice", fresh=True) == ("updated",)
    db._drop_connection.assert_called_once_with(Path("/tmp/message.db"))
    assert db._table_sequences == {("existing", "Msg_test"): 148}


def test_native_transcript_is_flushed_by_leaving_and_returning(monkeypatch):
    import wechat_ai_bot.rpa.action_handlers.transcribe_voice_handler as module
    item = handler()
    item.controller.db.user_info = SimpleNamespace(account="me")
    item.controller.db.get_contact_by_username.return_value = SimpleNamespace(username="me", display_name="Myself")
    item._title_matches = Mock(return_value=True)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    item._flush_client_transcript(action())
    assert [call.args for call in item.window_manager.switch_session.call_args_list] == [("Myself",), ("Alice",)]


def test_failed_transcription_retries_at_queue_tail_then_delivers_success_once():
    queue, deliver = Queue(), Mock()
    service = VoiceTranscriptionService(queue, enabled=True)
    message = voice()
    assert service.submit(message, deliver)
    first = queue.get_nowait()
    unrelated = object()
    queue.put_nowait(unrelated)
    first.execution_result.update(error="menu mismatch")
    first.on_complete()
    first.on_complete()
    deliver.assert_not_called()
    assert service.get_status()["pending"] == 1
    assert service.submit(voice(), deliver)
    assert queue.get_nowait() is unrelated
    retry = queue.get_nowait()
    assert (retry.server_id, retry.target_username, retry.message_db_path) == (
        first.server_id, first.target_username, first.message_db_path)
    retry.execution_result.update(verified=True, voice_to_text="重试成功")
    retry.on_complete()
    retry.on_complete()
    first.on_complete()
    deliver.assert_called_once_with(message)
    assert message.audio_text == "重试成功"
    assert queue.empty()
    assert service.get_status() == dict(enabled=True, pending=0, completed=1, retried=1, failed=0)


def test_retry_queue_full_finishes_without_stuck_pending_or_duplicate_delivery():
    queue, deliver = Queue(maxsize=1), Mock()
    service = VoiceTranscriptionService(queue, enabled=True)
    message = voice()
    service.submit(message, deliver)
    first = queue.get_nowait()
    unrelated = object()
    queue.put_nowait(unrelated)
    first.on_complete()
    first.on_complete()
    deliver.assert_called_once_with(message)
    assert service.get_status() == dict(enabled=True, pending=0, completed=1, retried=0, failed=1)
    assert queue.get_nowait() is unrelated


@pytest.mark.parametrize("items", [
    [{"label": "图语音转文字", "pixel_bbox": [10, 20, 110, 40]}],
    [{"label": "图收藏", "pixel_bbox": [10, 80, 80, 100]},
     {"label": "语音转文", "pixel_bbox": [10, 20, 110, 40]}],
    [{"label": "图收藏", "pixel_bbox": [200, 80, 280, 100]},
     {"label": "图语音转文字", "pixel_bbox": [10, 20, 110, 40]}],
    [{"label": "图收藏"}, {"label": "图语音转文字"}],
])
def test_menu_misses_log_ocr_labels_without_clicking(monkeypatch, caplog, items):
    import wechat_ai_bot.rpa.action_handlers.transcribe_voice_handler as module
    item = handler()
    item.image_processor = Mock()
    item.ocr_processor = Mock()
    item.ocr_processor.process_image.return_value = items
    mouse = Mock()
    mouse.position.return_value = (400, 1600)
    mouse.size.return_value = SimpleNamespace(width=1920, height=1916)
    monkeypatch.setattr(module, "mouse", mouse)
    with caplog.at_level(logging.WARNING):
        assert not item._click_transcribe_menu()
    mouse.click.assert_not_called()
    assert "OCR labels=" in caplog.text
    assert items[0]["label"] in caplog.text
