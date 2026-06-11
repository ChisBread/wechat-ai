import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


_STUB_MODULES = (
    "wechat_ai_bot.rpa.action_handlers.base_handler",
    "wechat_ai_bot.rpa.action_handlers.mixins.window_operations_mixin",
)
_ORIGINAL_MODULES = {name: sys.modules.get(name) for name in _STUB_MODULES}


def _install_stub_module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


base_handler = _install_stub_module("wechat_ai_bot.rpa.action_handlers.base_handler")


class DummyRPAAction:
    pass


class DummyRPAActionType:
    PAT = "pat"


class DummyBaseActionHandler:
    def __init__(self, controller=None):
        self.controller = controller


base_handler.BaseActionHandler = DummyBaseActionHandler
base_handler.RPAAction = DummyRPAAction
base_handler.RPAActionType = DummyRPAActionType

window_operations = _install_stub_module(
    "wechat_ai_bot.rpa.action_handlers.mixins.window_operations_mixin"
)


class DummyWindowOperationsMixin:
    pass


window_operations.WindowOperationsMixin = DummyWindowOperationsMixin

module_path = (
    Path(__file__).parents[1]
    / "src"
    / "wechat_ai_bot"
    / "rpa"
    / "action_handlers"
    / "pat_handler.py"
)
spec = importlib.util.spec_from_file_location("_pat_handler", module_path)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)
for module_name, original_module in _ORIGINAL_MODULES.items():
    if original_module is None:
        sys.modules.pop(module_name, None)
    else:
        sys.modules[module_name] = original_module
PatHandler = module.PatHandler


class PatHandlerTest(unittest.TestCase):
    def test_avatar_safe_click_point_biases_inside_left_half(self):
        handler = PatHandler.__new__(PatHandler)

        point = handler._avatar_safe_click_point([18.4, 204.2, 56.8, 246.9])

        self.assertEqual(point, (33, 225))

    def test_execute_right_clicks_safe_avatar_point(self):
        handler = PatHandler.__new__(PatHandler)
        handler.window_manager = types.SimpleNamespace(
            switch_session=lambda _target: True,
            get_message_region=lambda: [272, 69, 736, 1601],
        )
        handler.image_processor = types.SimpleNamespace(
            take_screenshot=lambda region, save_path=None: object(),
        )
        handler.logger = types.SimpleNamespace(
            error=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
            info=lambda *_args, **_kwargs: None,
        )
        handler.controller = types.SimpleNamespace(
            window_manager=types.SimpleNamespace(action_delay=0),
        )
        handler._cleanup = lambda: None
        handler._find_avatar_for_pat = lambda _screenshot, _action: {
            "pixel_bbox": [18.4, 204.2, 56.8, 246.9]
        }
        handler.find_and_click_menu_item = lambda _text: True

        with (
            patch.object(module, "human_like_mouse_move") as move,
            patch.object(module.pyautogui, "click") as click,
        ):
            ok = handler.execute(types.SimpleNamespace(target="Room", user_name="Bread", is_chatroom=True))

        self.assertTrue(ok)
        move.assert_called_once_with(305, 294)
        click.assert_called_once_with(305, 294, button="right")

    def test_find_name_candidates_falls_back_to_raw_ocr_for_single_character(self):
        handler = PatHandler.__new__(PatHandler)

        class DummyOCR:
            def find_text(self, image, target_text):
                return []

            def process_image(self, image):
                return [
                    {"label": "Bread", "pixel_bbox": [72.0, 211.0, 102.0, 221.0]},
                    {"label": "懒", "pixel_bbox": [69.0, 800.0, 81.0, 814.0]},
                ]

            def _calculate_text_similarity(self, left, right):
                return 1.0 if str(left).strip() == str(right).strip() else 0.0

        handler.ocr_processor = DummyOCR()

        matches = handler._find_name_candidates_for_pat(object(), "懒")

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["label"], "懒")
        self.assertEqual(matches[0]["source"], "raw")


if __name__ == "__main__":
    unittest.main()
