import base64
import io
import json
import logging
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest
from PIL import Image

from tests.test_debug_routes import DummyConfig, DummyMcpBot
from tests.test_send_verification import db, prepared, row
from wechat_ai_bot.mcp.app import AppContext, create_app
from wechat_ai_bot.mcp.dispatchers import QueueCommandDispatcher
from wechat_ai_bot.models import UserInfo
from wechat_ai_bot.rpa.action_handlers import SendVoiceAction, SendVoiceHandler
from wechat_ai_bot.rpa.action_handlers.send_voice_handler import VoiceControls
from wechat_ai_bot.rpa.send_verifier import SendVerificationError
from wechat_ai_bot.rpa.voice_audio import prepare_voice, PulseVoiceSource, MAX_AUDIO_BYTES
from wechat_ai_bot.services.core.rpa_service import RPAService
from wechat_ai_bot.weixin.message_classes import MessageType


def wav_bytes(seconds=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(b"\x00\x01" * int(seconds * 48000))
    return output.getvalue()


@pytest.mark.parametrize("seconds", [0.1, 0.99, 55.01, 60])
def test_audio_duration_rejected_without_truncation(tmp_path, seconds):
    with pytest.raises(ValueError, match="between 1 and 55"):
        prepare_voice(tmp_path, audio_base64=base64.b64encode(wav_bytes(seconds)).decode())


@pytest.mark.parametrize("seconds", [1, 55])
def test_audio_duration_boundaries(tmp_path, seconds):
    path, metadata = prepare_voice(tmp_path, audio_base64=base64.b64encode(wav_bytes(seconds)).decode())
    assert path.is_file()
    assert metadata["duration_seconds"] == seconds


@pytest.mark.parametrize("kwargs", [{}, {"audio_base64": "@@"}, {"audio_base64": "data:text/plain;base64,YQ=="},
    {"audio_base64": "YQ==", "file_path": "a.wav"}, {"file_path": "https://example.com/audio.mp3"},
    {"audio_base64": base64.b64encode(b"not audio").decode()},
    {"audio_base64": "A" * (4 * ((MAX_AUDIO_BYTES + 2) // 3) + 129)}])
def test_invalid_audio(tmp_path, kwargs):
    with pytest.raises(ValueError):
        prepare_voice(tmp_path, **kwargs)


def test_data_uri_and_snapshot(tmp_path):
    source = tmp_path / "original.wav"
    source.write_bytes(wav_bytes())
    staged, meta = prepare_voice(tmp_path, file_path=str(source))
    source.write_bytes(b"changed")
    assert wave.open(str(staged)).getnframes() == 96000
    staged, meta = prepare_voice(tmp_path, audio_base64="data:audio/wav;base64," + base64.b64encode(wav_bytes()).decode())
    assert meta["duration_seconds"] == 2


@pytest.fixture
def voice_tool(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setenv("WECHAT_AI_MCP_WRITE_ENABLED", "true")
    original = tempfile.TemporaryDirectory
    monkeypatch.setattr("wechat_ai_bot.mcp.app.tempfile.TemporaryDirectory",
                        lambda **kw: original(dir=tmp_path, **kw))
    bot = DummyMcpBot()
    bot.rpa_task_queue = Queue()
    user = UserInfo(account="synthetic")
    dispatcher = QueueCommandDispatcher(bot, user)
    app = create_app(user, DummyConfig({"debug": {"enabled": False}}), bot=bot)
    context = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=AppContext(user, dispatcher, {}, {})))

    def call(**kw):
        args = {"recipient_name": "Alice", "audio_base64": base64.b64encode(wav_bytes()).decode(), **kw}
        return json.loads(app._tool_manager._tools["send_voice_msg"].fn(context, **args))

    return SimpleNamespace(call=call, bot=bot, path=tmp_path, context=context)


def test_mcp_dry_run_and_write_gate(voice_tool, monkeypatch):
    monkeypatch.setenv("WECHAT_AI_MCP_WRITE_ENABLED", "false")
    assert voice_tool.call()["status"] == "blocked"
    result = voice_tool.call(dry_run=True)
    assert result["status"] == "dry_run"
    assert result["audio"]["duration_seconds"] == 2
    assert "file_path" not in result["action_data"]
    assert not list(voice_tool.path.iterdir())
    assert voice_tool.bot.rpa_task_queue.empty()


def test_mcp_invalid_audio_and_missing_target(voice_tool):
    assert voice_tool.call(audio_base64="invalid")["status"] == "invalid_request"
    assert voice_tool.call(recipient_name="")["status"] == "invalid_request"
    assert not list(voice_tool.path.iterdir())
    assert voice_tool.bot.rpa_task_queue.empty()


@pytest.mark.parametrize("wait", [0, 0.001])
def test_mcp_pending_preserves_audio_until_worker_cleanup(voice_tool, wait):
    result = voice_tool.call(wait_seconds=wait)
    assert result["status"] in {"queued", "timeout"}
    action = voice_tool.bot.rpa_task_queue.get_nowait()
    assert isinstance(action, SendVoiceAction)
    assert action.target == "Alice" and action.target_username == "alice"
    path = Path(action.file_path)
    assert path.is_file()
    action.on_complete()
    assert not path.exists()


@pytest.mark.parametrize("success", [True, False])
def test_worker_completion_cleans_staged_voice(voice_tool, success):
    def execute(action):
        assert Path(action.file_path).is_file()
        action.execution_result = {"verified": success}
        return success
    service = RPAService(voice_tool.bot.rpa_task_queue, SimpleNamespace(execute_action=execute))
    service.start()
    try:
        assert voice_tool.call(wait_seconds=3)["status"] == ("executed" if success else "failed")
    finally:
        service.stop()
    assert not list(voice_tool.path.iterdir())


def test_mcp_remote_dispatcher_rejected_and_cleans(voice_tool):
    voice_tool.context.request_context.lifespan_context.command_dispatcher = Mock()
    assert voice_tool.call()["status"] == "unavailable"
    assert not list(voice_tool.path.iterdir())


@pytest.mark.parametrize("candidate", [
    row(kind=MessageType.Audio, text='<msg><voicemsg voicelength="3000"/></msg>', local_id=1),
    row(kind=MessageType.Audio, text='<msg><voicemsg voicelength="3000"/></msg>', server_id=0),
    row(kind=MessageType.Audio, text='<msg><voicemsg voicelength="3000"/></msg>', sender_id=2),
    row(kind=MessageType.Audio, text='<msg><voicemsg voicelength="30000"/></msg>'),
    row(kind=MessageType.Audio, text='<msg><voicemsg voicelength="nan"/></msg>'),
    row(kind=MessageType.Audio, text='<msg/>'),
    row(kind=MessageType.File, text='<msg><voicemsg voicelength="3000"/></msg>'),
])
def test_voice_verification_rejects_wrong_pending_old_and_malformed(db, candidate):
    action = SendVoiceAction(target="Alice")
    action.recording_seconds = 3
    verifier, _ = prepared(db, action, baseline=[row(local_id=1)])
    db.get_messages_by_username.return_value = [candidate]
    with pytest.raises(SendVerificationError):
        verifier.verify(action)


def test_voice_verification_reports_metadata_limit_and_ambiguity(db):
    action = SendVoiceAction(target="Alice")
    action.recording_seconds = 3
    verifier, _ = prepared(db, action)
    db.get_messages_by_username.return_value = [row(kind=MessageType.Audio, text='<msg><voicemsg voicelength="3200"/></msg>')]
    result = verifier.verify(action)
    assert result["verification"] == "database_voice_metadata"
    assert result["audio_content_verified"] is False
    db.get_messages_by_username.return_value.append(row(local_id=3, kind=MessageType.Audio, text='<msg><voicemsg voicelength="3100"/></msg>'))
    with pytest.raises(SendVerificationError, match="ambiguous"):
        verifier.verify(action)


def test_template_matching_rejects_unknown_ui_and_tracks_window_offset():
    assets = Path(__file__).parents[1] / "src/wechat_ai_bot/rpa/assets"
    screenshot = Image.new("RGB", (320, 70), "white")
    screenshot.paste(Image.open(assets / "voice_start.png"), (150, 20))
    wm = SimpleNamespace(refresh_window_geometry=lambda: {"x": 100, "y": 50, "width": 1008, "height": 900})
    ip = SimpleNamespace(take_screenshot=lambda **kw: screenshot)
    controls = VoiceControls(wm, ip)
    assert controls.locate("start") == [(948, 910)]
    with pytest.raises(RuntimeError, match="not recognized"):
        controls.locate("send")


@pytest.mark.parametrize("failure", [None, "bind", "play", "controls", "deadline"])
def test_recording_failure_cancels_and_never_sends(tmp_path, monkeypatch, failure):
    module = "wechat_ai_bot.rpa.action_handlers.send_voice_handler"
    wav = tmp_path / "voice.wav"
    wav.write_bytes(wav_bytes())
    clicks = []
    monkeypatch.setattr(module + ".mouse.click", lambda *xy: clicks.append(xy))
    monkeypatch.setattr(module + ".time.sleep", lambda _: None)
    audio = Mock()
    audio.__enter__ = Mock(return_value=audio)
    audio.__exit__ = Mock(return_value=False)
    if failure == "deadline":
        timers = []
        def timer(delay, callback):
            timers.append(callback)
            return Mock()
        monkeypatch.setattr(module + ".Timer", timer)
        audio.play.side_effect = lambda *args: timers[0]()
    if failure in {"bind", "play"}:
        getattr(audio, "bind_recording" if failure == "bind" else "play").side_effect = RuntimeError("probe failure")
    monkeypatch.setattr(module + ".PulseVoiceSource", lambda: audio)
    controls = Mock()
    def locate(*names):
        if failure == "controls" and "send" in names:
            raise RuntimeError("not recognized")
        return [{"start": (1, 1), "cancel": (2, 2), "send": (3, 3)}[name] for name in names]
    controls.locate.side_effect = locate
    monkeypatch.setattr(module + ".VoiceControls", lambda *args: controls)
    controller = SimpleNamespace(logger=logging.getLogger("voice-test"), window_manager=Mock(),
                                 image_processor=Mock(), ocr_processor=Mock(), input_handler=Mock(), ui_helper=Mock())
    action = SendVoiceAction(file_path=str(wav), target="Alice")
    handler = SendVoiceHandler(controller)
    if failure:
        with pytest.raises(RuntimeError):
            handler.execute(action)
        assert (2, 2) in clicks and (3, 3) not in clicks
    else:
        assert handler.execute(action)
        assert clicks == [(1, 1), (3, 3)]
    audio.__exit__.assert_called_once()


def test_pulse_partial_setup_failure_unloads_owned_modules(monkeypatch):
    audio = PulseVoiceSource()
    calls = []
    def pactl(*args):
        calls.append(args)
        if args[:2] == ("-f", "json"):
            return "[]"
        if args[0] == "get-default-source":
            return "original"
        if args[:2] == ("load-module", "module-null-sink"):
            return "42"
        if args[:2] == ("load-module", "module-remap-source"):
            raise RuntimeError("module unavailable")
        return ""
    monkeypatch.setattr(audio, "pactl", pactl)
    with pytest.raises(RuntimeError, match="module unavailable"):
        with audio:
            pass
    assert ("unload-module", "42") in calls
    assert not any(call[0] == "set-default-source" for call in calls)


def test_pulse_refuses_existing_wechat_recording(monkeypatch):
    audio = PulseVoiceSource()
    monkeypatch.setattr(audio, "streams", lambda: [{"index": 1}])
    pactl = Mock()
    monkeypatch.setattr(audio, "pactl", pactl)
    with pytest.raises(RuntimeError, match="already recording"):
        with audio:
            pass
    pactl.assert_not_called()


@pytest.mark.parametrize("current_default", ["ours", "changed-by-user"])
def test_pulse_restores_only_its_default_and_unloads_reverse_order(monkeypatch, current_default):
    audio = PulseVoiceSource()
    audio.original_source = "original"
    audio.modules = ["sink-module", "source-module"]
    def pactl(*args):
        return audio.source if current_default == "ours" else "user-microphone"
    run = Mock(side_effect=pactl)
    monkeypatch.setattr(audio, "pactl", run)
    audio.__exit__(None, None, None)
    args = [call.args for call in run.call_args_list]
    assert (("set-default-source", "original") in args) == (current_default == "ours")
    assert args[-2:] == [("unload-module", "source-module"), ("unload-module", "sink-module")]


def test_pulse_route_change_prevents_submission(monkeypatch):
    audio = PulseVoiceSource()
    audio.stream_id = 12
    monkeypatch.setattr(audio, "pactl", lambda *args: json.dumps([{"name": audio.source, "index": 4}]))
    monkeypatch.setattr(audio, "streams", lambda: [{"index": 12, "source": 5}])
    with pytest.raises(RuntimeError, match="route changed"):
        audio.check_recording()


def test_filehelper_chooses_function_instead_of_web_result(monkeypatch):
    module = "wechat_ai_bot.rpa.action_handlers.send_voice_handler"
    controller = SimpleNamespace(logger=logging.getLogger("voice-test"), window_manager=Mock(),
                                 image_processor=Mock(), ocr_processor=Mock(), input_handler=Mock(), ui_helper=Mock())
    controller.window_manager.refresh_window_geometry.return_value = {"x": 0, "y": 0, "width": 1008, "height": 900}
    controller.ocr_processor.process_image.return_value = [
        {"label": "文件传输助手", "pixel_bbox": [80, 20, 180, 40]},
        {"label": "功能", "pixel_bbox": [80, 150, 110, 170]},
        {"label": "文件传输助手", "pixel_bbox": [80, 180, 180, 200]},
    ]
    click = Mock()
    monkeypatch.setattr(module + ".mouse.click", click)
    hotkey = Mock()
    monkeypatch.setattr(module + ".mouse.hotkey", hotkey)
    monkeypatch.setattr(module + ".set_clipboard_text", lambda text: True)
    monkeypatch.setattr(module + ".time.sleep", lambda _: None)
    assert SendVoiceHandler(controller)._switch_session(SendVoiceAction(target="文件传输助手", target_username="filehelper"))
    click.assert_called_once_with(130, 270)
    assert all("enter" not in c.args for c in hotkey.call_args_list)
    controller.window_manager.switch_session.assert_not_called()
