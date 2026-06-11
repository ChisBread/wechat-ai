import sys
import types
import unittest
import importlib.util
from pathlib import Path
from importlib.util import find_spec
from enum import Enum
from unittest.mock import patch


def _module_exists(module_name):
    try:
        return find_spec(module_name) is not None
    except (ImportError, AttributeError, ValueError):
        return module_name in sys.modules


if not _module_exists("cv2"):
    sys.modules.setdefault("cv2", types.ModuleType("cv2"))


class DummyWindowTypeEnum(Enum):
    MenuWindow = "MenuWindow"


linux_window_manager = types.ModuleType("wechat_ai_bot.rpa.linux_window_manager")
linux_window_manager.WindowTypeEnum = DummyWindowTypeEnum
original_linux_window_manager = sys.modules.get("wechat_ai_bot.rpa.linux_window_manager")
sys.modules["wechat_ai_bot.rpa.linux_window_manager"] = linux_window_manager
module_path = (
    Path(__file__).parents[1]
    / "src"
    / "wechat_ai_bot"
    / "rpa"
    / "action_handlers"
    / "mixins"
    / "window_operations_mixin.py"
)
spec = importlib.util.spec_from_file_location("_window_operations_mixin", module_path)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)
if original_linux_window_manager is None:
    sys.modules.pop("wechat_ai_bot.rpa.linux_window_manager", None)
else:
    sys.modules["wechat_ai_bot.rpa.linux_window_manager"] = original_linux_window_manager
WindowOperationsMixin = module.WindowOperationsMixin


class DummyImageProcessor:
    def __init__(self):
        self.screenshots = []

    def take_screenshot(self, region, save_path=None):
        self.screenshots.append((region, save_path))
        return object()


class DummyOCRProcessor:
    def process_image(self, image):
        return [{"label": "拍一拍", "pixel_bbox": [30, 40, 90, 70]}]


class DummyWindowManager:
    action_delay = 0
    window_margin = 20

    def wait_for_window(self, _window_type):
        return None


class DummyLogger:
    def info(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class DummyController:
    def __init__(self):
        self.image_processor = DummyImageProcessor()
        self.ocr_processor = DummyOCRProcessor()
        self.window_manager = DummyWindowManager()


class DummyMixin(WindowOperationsMixin):
    def __init__(self):
        self.controller = DummyController()
        self.ocr_processor = self.controller.ocr_processor
        self.logger = DummyLogger()


class WindowOperationsMixinTest(unittest.TestCase):
    def test_menu_item_fallback_ocr_near_mouse(self):
        helper = DummyMixin()

        with (
            patch.object(module.pyautogui, "position", return_value=(200, 300)),
            patch.object(module.pyautogui, "size", return_value=types.SimpleNamespace(width=1008, height=1820)),
            patch.object(module, "human_like_mouse_move") as move,
            patch.object(module.pyautogui, "click") as click,
        ):
            ok = helper.find_and_click_menu_item("拍一拍")

        self.assertTrue(ok)
        self.assertEqual(helper.controller.image_processor.screenshots[0][0], [120, 220, 420, 620])
        move.assert_called_once_with(target_x=180, target_y=275)
        click.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
