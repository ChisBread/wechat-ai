import sys
import types
import unittest
from importlib.util import find_spec
from unittest.mock import Mock, patch


def _module_exists(module_name):
    try:
        return find_spec(module_name) is not None
    except (ImportError, AttributeError, ValueError):
        return module_name in sys.modules


for module_name in ("cv2", "mss", "mss.tools", "requests", "torch"):
    if not _module_exists(module_name):
        sys.modules.setdefault(module_name, types.ModuleType(module_name))

if not _module_exists("numpy"):
    numpy = types.ModuleType("numpy")
    numpy.array = lambda value: value
    numpy.uint8 = object
    sys.modules.setdefault("numpy", numpy)

if not _module_exists("ultralytics"):
    ultralytics = types.ModuleType("ultralytics")
    ultralytics.YOLO = object
    sys.modules.setdefault("ultralytics", ultralytics)

if not _module_exists("rapidocr"):
    rapidocr = types.ModuleType("rapidocr")
    rapidocr.RapidOCR = object
    sys.modules.setdefault("rapidocr", rapidocr)

from wechat_ai_bot.rpa.linux_window_manager import WindowTypeEnum
from wechat_ai_bot.rpa.xfce_window_manager import XFCEWindowManager


class DummyCompletedProcess:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class DummyImageProcessor:
    def take_screenshot(self, region, save_path=None):
        return object()


class DummyOCRProcessor:
    def __init__(self, labels=None):
        self.labels = labels or []

    def process_image(self, image):
        return [{"label": label, "pixel_bbox": [0, 0, 10, 10]} for label in self.labels]


class XFCEWindowManagerTest(unittest.TestCase):
    def test_menu_window_search_includes_unmanaged_wechat_popup(self):
        manager = XFCEWindowManager(image_processor=object(), ocr_processor=object())
        manager.size_config.width = 1008
        manager.size_config.height = 1820

        def fake_run(*args):
            if args == ("xprop", "-root", "_NET_CLIENT_LIST"):
                return DummyCompletedProcess("0xa00013")
            if args == ("xdotool", "getwindowname", "10485779"):
                return DummyCompletedProcess("WeChat")
            if args == ("xdotool", "getwindowname", "10485890"):
                return DummyCompletedProcess("wechat")
            if args[:3] == ("xdotool", "search", "--class"):
                return DummyCompletedProcess("10485779\n10485890\n")
            if args[:3] == ("xdotool", "search", "--name"):
                return DummyCompletedProcess("")
            return DummyCompletedProcess("")

        def fake_geometry(wid):
            if str(wid) == "10485779":
                return {"x": 0, "y": 0, "width": 1008, "height": 1820}
            if str(wid) == "10485890":
                return {"x": 240, "y": 680, "width": 188, "height": 360}
            return None

        with (
            patch("wechat_ai_bot.rpa.xfce_window_manager._run", side_effect=fake_run),
            patch.object(manager, "_window_geometry", side_effect=fake_geometry),
        ):
            window = manager.get_window(WindowTypeEnum.MenuWindow)

        self.assertIsNotNone(window)
        self.assertEqual(window.id, "10485890")
        self.assertEqual(window.title, "wechat")

    def test_confirm_window_search_includes_unmanaged_wechat_popup(self):
        manager = XFCEWindowManager(image_processor=object(), ocr_processor=object())
        manager.size_config.width = 1008
        manager.size_config.height = 1820

        def fake_run(*args):
            if args == ("xdotool", "getactivewindow"):
                return DummyCompletedProcess("")
            if args == ("xprop", "-root", "_NET_CLIENT_LIST"):
                return DummyCompletedProcess("0x600013")
            if args == ("xdotool", "getwindowname", "6291457"):
                return DummyCompletedProcess("WeChat")
            if args == ("xdotool", "getwindowname", "6291526"):
                return DummyCompletedProcess("wechat")
            if args[:3] == ("xdotool", "search", "--class"):
                return DummyCompletedProcess("6291457\n6291526\n")
            if args[:3] == ("xdotool", "search", "--name"):
                return DummyCompletedProcess("")
            return DummyCompletedProcess("")

        def fake_geometry(wid):
            if str(wid) == "6291457":
                return {"x": 0, "y": 0, "width": 1008, "height": 1820}
            if str(wid) == "6291526":
                return {"x": 363, "y": 833, "width": 282, "height": 153}
            return None

        with (
            patch("wechat_ai_bot.rpa.xfce_window_manager._run", side_effect=fake_run),
            patch.object(manager, "_window_geometry", side_effect=fake_geometry),
        ):
            window = manager.get_window(WindowTypeEnum.RoomInputConfirmBox)

        self.assertIsNotNone(window)
        self.assertEqual(window.id, "6291526")
        self.assertEqual(window.title, "wechat")

    def test_confirm_window_prefers_active_wechat_popup_geometry(self):
        manager = XFCEWindowManager(image_processor=object(), ocr_processor=object())
        manager.size_config.width = 1008
        manager.size_config.height = 1820

        def fake_run(*args):
            if args == ("xdotool", "getactivewindow"):
                return DummyCompletedProcess("6291553")
            if args == ("xdotool", "getwindowname", "6291553"):
                return DummyCompletedProcess("wechat")
            return DummyCompletedProcess("")

        def fake_geometry(wid):
            if str(wid) == "6291553":
                return {"x": 363, "y": 833, "width": 282, "height": 153}
            return None

        with (
            patch("wechat_ai_bot.rpa.xfce_window_manager._run", side_effect=fake_run),
            patch.object(manager, "_window_geometry", side_effect=fake_geometry),
        ):
            window = manager.get_window(WindowTypeEnum.RoomInputConfirmBox)

        self.assertIsNotNone(window)
        self.assertEqual(window.left, 363)
        self.assertEqual(window.top, 833)

    def test_confirm_window_ignores_tiny_wechat_helper_window(self):
        manager = XFCEWindowManager(image_processor=object(), ocr_processor=object())
        manager.size_config.width = 1008
        manager.size_config.height = 1820

        def fake_run(*args):
            if args == ("xdotool", "getactivewindow"):
                return DummyCompletedProcess("")
            if args == ("xprop", "-root", "_NET_CLIENT_LIST"):
                return DummyCompletedProcess("")
            if args == ("xdotool", "getwindowname", "6291457"):
                return DummyCompletedProcess("WeChat")
            if args == ("xdotool", "getwindowname", "6291500"):
                return DummyCompletedProcess("wechat")
            if args == ("xdotool", "getwindowname", "6291526"):
                return DummyCompletedProcess("wechat")
            if args[:3] == ("xdotool", "search", "--class"):
                return DummyCompletedProcess("6291457\n6291500\n6291526\n")
            if args[:3] == ("xdotool", "search", "--name"):
                return DummyCompletedProcess("")
            return DummyCompletedProcess("")

        def fake_geometry(wid):
            if str(wid) == "6291457":
                return {"x": 0, "y": 0, "width": 1008, "height": 1820}
            if str(wid) == "6291500":
                return {"x": 0, "y": 0, "width": 160, "height": 160}
            if str(wid) == "6291526":
                return {"x": 363, "y": 833, "width": 282, "height": 153}
            return None

        with (
            patch("wechat_ai_bot.rpa.xfce_window_manager._run", side_effect=fake_run),
            patch.object(manager, "_window_geometry", side_effect=fake_geometry),
        ):
            window = manager.get_window(WindowTypeEnum.RoomInputConfirmBox)

        self.assertIsNotNone(window)
        self.assertEqual(window.id, "6291526")
        self.assertEqual(window.left, 363)
        self.assertEqual(window.top, 833)

    def test_close_all_windows_closes_lowercase_wechat_popup(self):
        manager = XFCEWindowManager(image_processor=object(), ocr_processor=object())
        manager.window_id = "6291475"
        manager.size_config.width = 1008
        manager.size_config.height = 1820
        main = types.SimpleNamespace(
            id="6291475",
            title="WeChat",
            width=1008,
            height=1820,
            close=Mock(),
        )
        popup = types.SimpleNamespace(
            id="6291601",
            title="wechat",
            width=282,
            height=153,
            close=Mock(),
        )

        with patch.object(manager, "_all_wechat_windows", return_value=[main, popup]):
            manager.close_all_windows()

        main.close.assert_not_called()
        popup.close.assert_called_once()

    def test_room_sidebar_state_requires_sidebar_markers(self):
        manager = XFCEWindowManager(
            image_processor=DummyImageProcessor(),
            ocr_processor=DummyOCRProcessor(labels=["@Bread 机器人测试", "发送(S)"]),
        )
        manager.size_config.width = 1008
        manager.size_config.height = 1820
        manager.TITLE_BAR_HEIGHT = 69

        self.assertFalse(manager._is_room_sidebar_open())

    def test_room_sidebar_state_detects_sidebar_markers(self):
        manager = XFCEWindowManager(
            image_processor=DummyImageProcessor(),
            ocr_processor=DummyOCRProcessor(labels=["搜索群成员", "群聊名称", "消息免打扰"]),
        )
        manager.size_config.width = 1008
        manager.size_config.height = 1820
        manager.TITLE_BAR_HEIGHT = 69

        self.assertTrue(manager._is_room_sidebar_open())

    def test_open_sidebar_clicks_wechat_more_button_below_titlebar_controls(self):
        manager = XFCEWindowManager(image_processor=object(), ocr_processor=object())
        manager.size_config.width = 1008
        manager.size_config.height = 1820
        manager.TITLE_BAR_HEIGHT = 69
        manager.MSG_TOP_X = 272
        manager.MSG_WIDTH = 736
        manager.side_bar_delay = 0

        with (
            patch.object(manager, "_is_room_sidebar_open", return_value=False),
            patch("wechat_ai_bot.rpa.xfce_window_manager.human_like_mouse_move") as move,
            patch("wechat_ai_bot.rpa.xfce_window_manager.pyautogui.click") as click,
        ):
            self.assertTrue(manager.open_close_sidebar())

        move.assert_called_once_with(978, 59)
        click.assert_called_once()

    def test_resize_wechat_window_continues_without_wmctrl(self):
        manager = XFCEWindowManager(image_processor=object(), ocr_processor=object())
        manager.action_delay = 0
        calls = []

        def fake_run(*args):
            calls.append(args)
            if args and args[0] == "wmctrl":
                raise FileNotFoundError("wmctrl")
            return DummyCompletedProcess("")

        with (
            patch("wechat_ai_bot.rpa.xfce_window_manager._run", side_effect=fake_run),
            patch.object(
                manager,
                "_window_geometry",
                return_value={"x": 0, "y": 0, "width": 1008, "height": 1820},
            ),
        ):
            geom = manager._resize_wechat_window("6291475", 1008, 1820)

        self.assertEqual(geom["width"], 1008)
        self.assertIn(("xdotool", "windowsize", "--sync", "6291475", "1008", "1820"), calls)


if __name__ == "__main__":
    unittest.main()
