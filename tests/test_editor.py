from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image, ImageDraw
import pytest

from wechat_ai_bot.rpa.editor import enabled_send_button


@pytest.mark.parametrize("active", [False, True])
def test_send_button_requires_enabled_pixels_and_excludes_voice_icon(active):
    image = Image.new("RGB", (98, 36), "#eeeeee")
    if active:
        ImageDraw.Draw(image).rectangle((47, 5, 94, 31), fill="#07c160")
    window = SimpleNamespace(
        get_icon_position=Mock(return_value=[892, 1770, 990, 1806]),
        image_processor=SimpleNamespace(take_screenshot=Mock(return_value=image)),
    )
    assert enabled_send_button(window) == ([939, 1775, 986, 1801] if active else None)


def test_failed_capture_never_counts_as_enabled():
    window = SimpleNamespace(
        get_icon_position=Mock(return_value=[892, 1770, 990, 1806]),
        image_processor=SimpleNamespace(take_screenshot=Mock(return_value=None)),
    )
    assert enabled_send_button(window) is None


@pytest.mark.parametrize("focused,pasted", [(False, True), (True, False), (True, True)])
def test_file_handler_clears_draft_and_only_proceeds_after_paste(focused, pasted, tmp_path, monkeypatch):
    import logging
    from wechat_ai_bot.rpa.action_handlers.send_file_handler import SendFileAction, SendFileHandler
    file = tmp_path / "image.png"
    Image.new("RGB", (8, 8)).save(file)
    window = SimpleNamespace(switch_session=Mock(return_value=True), close_all_windows=Mock(), action_delay=0)
    clear = Mock(return_value=focused)
    click = Mock(return_value=True)
    controller = SimpleNamespace(
        window_manager=window, message_sender=SimpleNamespace(clear_input_box=clear),
        ui_helper=SimpleNamespace(click_send_button=click), image_processor=None,
        ocr_processor=None, input_handler=None, logger=logging.getLogger(__name__),
    )
    monkeypatch.setattr("wechat_ai_bot.rpa.action_handlers.send_file_handler.time.sleep", lambda _: None)
    copy = Mock(return_value=pasted)
    monkeypatch.setattr("wechat_ai_bot.rpa.action_handlers.send_file_handler.copy_file_to_clipboard", copy)
    hotkey = Mock()
    monkeypatch.setattr("wechat_ai_bot.rpa.action_handlers.send_file_handler.pyautogui.hotkey", hotkey)
    assert SendFileHandler(controller).execute(SendFileAction(target="Room", file_path=str(file))) is (focused and pasted)
    clear.assert_called_once()
    if focused and pasted:
        hotkey.assert_called_once_with("ctrl", "v")
        click.assert_called_once()
    else:
        click.assert_not_called()
        hotkey.assert_not_called()
    window.close_all_windows.assert_called_once()
