import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from wechat_ai_bot.mcp.debug import _dashboard_html, build_layout_png, mask_value, redact_text, tail_file
from wechat_ai_bot.mcp.app import create_app
from wechat_ai_bot.models import UserInfo
from wechat_ai_bot.utils import size_config


class DummySize:
    width = 640
    height = 480


class DummyWindowManager:
    size_config = DummySize()
    SIDE_BAR_WIDTH = 64
    SESSION_LIST_WIDTH = 250
    MSG_TOP_X = 314
    MSG_TOP_Y = 70
    MSG_WIDTH = 326
    MSG_HEIGHT = 280
    ICON_CONFIGS = {"send_button": {"position": [540, 430, 620, 465]}}


class DummyBot:
    window_manager = DummyWindowManager()


class DummyConfig(dict):
    def get(self, key, default=None):
        if "." not in key:
            return super().get(key, default)
        value = self
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


class DebugRoutesTest(unittest.TestCase):
    def test_mask_value_keeps_edges(self):
        self.assertEqual(mask_value("wxid_abcdef"), "wx***ef")
        self.assertEqual(mask_value("abc"), "a***")
        self.assertEqual(mask_value(""), "")

    def test_redact_text_masks_common_secret_values(self):
        redacted = redact_text("api_key=sk-test password: hunter2 dbkey=abcdef")

        self.assertIn("api_key=***", redacted)
        self.assertIn("password: ***", redacted)
        self.assertIn("dbkey=***", redacted)
        self.assertNotIn("hunter2", redacted)

    def test_tail_file_returns_last_lines(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text("\n".join(f"line-{i}" for i in range(20)), encoding="utf-8")

            self.assertEqual(tail_file(path, lines=3), "line-17\nline-18\nline-19")

    def test_build_layout_png_returns_png_bytes(self):
        png = build_layout_png(DummyBot())

        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(png), 100)

    def test_create_app_registers_debug_routes_when_enabled(self):
        app = create_app(
            UserInfo(account="me"),
            DummyConfig({"debug": {"enabled": True}, "mcp": {"port": 8000}}),
            bot=DummyBot(),
        )
        paths = {route.path for route in app.streamable_http_app().routes}

        self.assertIn("/dashboard", paths)
        self.assertIn("/dashboard/api/status", paths)
        self.assertIn("/dashboard/api/contacts", paths)
        self.assertIn("/dashboard/api/messages", paths)
        self.assertIn("/dashboard/api/rpa/send_text", paths)
        self.assertIn("/debug", paths)
        self.assertIn("/debug/api/status", paths)
        self.assertIn("/debug/api/contacts", paths)
        self.assertIn("/debug/api/messages", paths)
        self.assertIn("/debug/api/rpa/send_text", paths)

    def test_dashboard_html_loads_manager_template(self):
        self.assertIn("WeChat-AI Manager", _dashboard_html())

    def test_create_app_skips_debug_routes_when_disabled(self):
        app = create_app(
            UserInfo(account="me"),
            DummyConfig({"debug": {"enabled": False}, "mcp": {"port": 8000}}),
            bot=DummyBot(),
        )
        paths = {route.path for route in app.streamable_http_app().routes}

        self.assertNotIn("/dashboard", paths)
        self.assertNotIn("/debug", paths)

    def test_suggest_size_aligns_configured_window_to_factor(self):
        original_size = size_config.pyautogui.size
        size_config.pyautogui.size = lambda: type("Screen", (), {"width": 3840, "height": 1916})()
        try:
            suggested = size_config.suggest_size(
                {
                    "window": {
                        "width": 1008,
                        "height": 0,
                        "min_height": 812,
                        "max_height": 2000,
                        "align_factor": 28,
                    }
                }
            )
        finally:
            size_config.pyautogui.size = original_size

        self.assertEqual(suggested.width, 1008)
        self.assertEqual(suggested.height, 1820)


if __name__ == "__main__":
    unittest.main()
