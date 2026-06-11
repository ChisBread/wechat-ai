import importlib.util
import sys
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import patch


class DummyBtnType(Enum):
    GREEN = "green"


class DummyWindowTypeEnum(Enum):
    RoomInputConfirmBox = "RoomInputConfirmBox"


ui_helper = types.ModuleType("wechat_ai_bot.rpa.ui_helper")
ui_helper.BtnType = DummyBtnType
linux_window_manager = types.ModuleType("wechat_ai_bot.rpa.linux_window_manager")
linux_window_manager.WindowTypeEnum = DummyWindowTypeEnum
_original_modules = {
    "wechat_ai_bot.rpa.ui_helper": sys.modules.get("wechat_ai_bot.rpa.ui_helper"),
    "wechat_ai_bot.rpa.linux_window_manager": sys.modules.get(
        "wechat_ai_bot.rpa.linux_window_manager"
    ),
}
sys.modules["wechat_ai_bot.rpa.ui_helper"] = ui_helper
sys.modules["wechat_ai_bot.rpa.linux_window_manager"] = linux_window_manager

module_path = (
    Path(__file__).parents[1]
    / "src"
    / "wechat_ai_bot"
    / "rpa"
    / "action_handlers"
    / "mixins"
    / "group_operations_mixin.py"
)
spec = importlib.util.spec_from_file_location("_group_operations_mixin", module_path)
group_operations_module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(group_operations_module)
for module_name, original_module in _original_modules.items():
    if original_module is None:
        sys.modules.pop(module_name, None)
    else:
        sys.modules[module_name] = original_module
GroupOperationsMixin = group_operations_module.GroupOperationsMixin


class DummySizeConfig:
    width = 1008
    height = 1820


class DummyWindowManager:
    size_config = DummySizeConfig()
    action_delay = 0
    scroll_delay = 0
    window_margin = 20
    rpa_config = {}

    def __init__(self, popup=None):
        if popup is None:
            self.popups = []
        elif isinstance(popup, list):
            self.popups = list(popup)
        else:
            self.popups = [popup]
        self.waited = []
        self.sidebar_calls = []

    def wait_for_window(self, window_type, timeout=5):
        self.waited.append((window_type, timeout))
        if not self.popups:
            return None
        return self.popups.pop(0)

    def get_window(self, window_type):
        self.waited.append((window_type, 0))
        if not self.popups:
            return None
        return self.popups.pop(0)

    def open_close_sidebar(self, close=False):
        self.sidebar_calls.append(close)
        return True


class DummyController:
    def __init__(self, popup=None):
        self.window_manager = DummyWindowManager(popup=popup)


class DummyGroupOperations(GroupOperationsMixin):
    def __init__(self, popup=None, text_candidate_success=True):
        self.controller = DummyController(popup=popup)
        self.window_manager = self.controller.window_manager
        self.clicked_regions = []
        self.clicked_texts = []
        self.text_candidate_success = text_candidate_success
        self.logger = types.SimpleNamespace(warning=lambda *_args, **_kwargs: None)
        self.ui_helper = types.SimpleNamespace(
            find_and_click_text_candidate=self._find_and_click_text_candidate,
            find_btn_by_text=lambda **_kwargs: None,
        )

    def get_window_region(self, _window):
        return [10, 20, 300, 180]

    def _find_and_click_text_candidate(self, texts, region, fuzzy=100, save_path=None):
        self.clicked_texts.append((texts, region, fuzzy, save_path))
        if not self.text_candidate_success:
            return None
        return [1, 2, 3, 4] if "修改" in texts else None


class GroupOperationsMixinTest(unittest.TestCase):
    def test_confirm_room_input_change_falls_back_to_center_overlay(self):
        helper = DummyGroupOperations()

        ok = helper._confirm_room_input_change(timeout=2)

        self.assertTrue(ok)
        self.assertEqual(helper.clicked_texts[0][0], ("确定", "完成", "保存", "修改", "确认"))
        self.assertEqual(helper.clicked_texts[0][1], [151, 650, 705, 520])
        self.assertEqual(helper.window_manager.waited[0][0], DummyWindowTypeEnum.RoomInputConfirmBox)

    def test_confirm_room_input_change_prefers_popup_window_region(self):
        helper = DummyGroupOperations(popup=object())

        with patch.object(helper, "_click_confirm_button", return_value=True) as click:
            ok = helper._confirm_room_input_change(timeout=2)

        self.assertTrue(ok)
        click.assert_called_once_with([10, 20, 300, 180])

    def test_confirm_room_input_change_polls_for_delayed_popup(self):
        helper = DummyGroupOperations(popup=[None, object()])

        with patch.object(helper, "_click_confirm_button", side_effect=[True]) as click:
            ok = helper._confirm_room_input_change(timeout=2)

        self.assertTrue(ok)
        click.assert_called_once_with([10, 20, 300, 180])

    def test_trigger_room_input_confirmation_closes_sidebar_when_no_popup(self):
        helper = DummyGroupOperations()

        helper._trigger_room_input_confirmation()

        self.assertEqual(helper.window_manager.sidebar_calls, [True])

    def test_trigger_room_input_confirmation_keeps_sidebar_when_popup_exists(self):
        helper = DummyGroupOperations(popup=object())

        helper._trigger_room_input_confirmation()

        self.assertEqual(helper.window_manager.sidebar_calls, [])

    def test_click_confirm_button_accepts_modify_text(self):
        helper = DummyGroupOperations()

        ok = helper._click_confirm_button([10, 20, 300, 180])

        self.assertTrue(ok)
        self.assertEqual(helper.clicked_texts[0][0], ("确定", "完成", "保存", "修改", "确认"))
        self.assertEqual(helper.clicked_texts[0][1], [10, 20, 300, 180])


if __name__ == "__main__":
    unittest.main()
