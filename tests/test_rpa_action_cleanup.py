import logging
import importlib.util
import unittest
from pathlib import Path

module_path = (
    Path(__file__).parents[1]
    / "src"
    / "wechat_ai_bot"
    / "rpa"
    / "action_handlers"
    / "base_handler.py"
)
spec = importlib.util.spec_from_file_location("_base_handler", module_path)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)
BaseActionHandler = module.BaseActionHandler


class DummyWindowManager:
    def __init__(self):
        self.closed = False
        self.sidebar_calls = []

    def close_all_windows(self):
        self.closed = True

    def open_close_sidebar(self, close=False):
        self.sidebar_calls.append(close)


class DummyController:
    def __init__(self):
        self.window_manager = DummyWindowManager()
        self.image_processor = object()
        self.ocr_processor = object()
        self.input_handler = object()
        self.ui_helper = object()
        self.logger = logging.getLogger("test")


class DummyHandler(BaseActionHandler):
    def execute(self, action):
        return True


class BaseActionHandlerCleanupTest(unittest.TestCase):
    def test_cleanup_does_not_touch_sidebar_by_default(self):
        controller = DummyController()
        handler = DummyHandler(controller)

        handler._cleanup()

        self.assertTrue(controller.window_manager.closed)
        self.assertEqual(controller.window_manager.sidebar_calls, [])

    def test_cleanup_can_close_sidebar_explicitly(self):
        controller = DummyController()
        handler = DummyHandler(controller)

        handler._cleanup(close_sidebar=True)

        self.assertTrue(controller.window_manager.closed)
        self.assertEqual(controller.window_manager.sidebar_calls, [True])


if __name__ == "__main__":
    unittest.main()
