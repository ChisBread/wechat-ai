import sys
import types
import unittest
from importlib.util import find_spec
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw


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


class WindowManagerFixture(unittest.TestCase):
    def setUp(self):
        size = types.SimpleNamespace(width=1008, height=1820)
        with patch("wechat_ai_bot.rpa.xfce_window_manager.suggest_size", return_value=size):
            self.manager = XFCEWindowManager(Mock(), Mock())
        self.manager.rpa_config["layout"] = {"startup_session": ""}
        self.manager._save_layout_debug = Mock()
        self.manager.window_show_delay = self.manager.scroll_delay = 0

    def frame(self, input_height=141, contrast=30, title=True, editor=True):
        image = Image.new("RGB", (1008, 1820), (240, 240, 240))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 60, 1819), fill=(40, 40, 40))
        draw.rectangle((61, 0, 300, 1819), fill=(220, 220, 220))
        shade = (240 - contrast,) * 3
        if title:
            draw.rectangle((301, 0, 1007, 79), fill=shade)
        if editor:
            draw.rectangle((301, 1820 - input_height, 1007, 1819), fill=shade)
        return image

    def mock_window(self):
        """Keep X11 interactions inert while exercising real layout/retry paths."""
        geometry = {"x": 0, "y": 0, "width": 1008, "height": 1820}
        patches = [
            patch("wechat_ai_bot.rpa.xfce_window_manager.subprocess.run",
                  return_value=DummyCompletedProcess("123")),
            patch("wechat_ai_bot.rpa.xfce_window_manager._run",
                  return_value=DummyCompletedProcess()),
            patch("wechat_ai_bot.rpa.xfce_window_manager.time.sleep"),
            patch.object(self.manager, "_find_wechat_window", return_value="123"),
            patch.object(self.manager, "_enter_wechat_if_needed", return_value=True),
            patch.object(self.manager, "_normalize_dpi"),
            patch.object(self.manager, "_target_window_size", return_value=(1008, 1820)),
            patch.object(self.manager, "_window_geometry", return_value=geometry),
            patch.object(self.manager, "_resize_wechat_window", return_value=geometry),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_unready(self):
        self.assertIsNone(self.manager.current_window)
        self.assertNotIn("WeChat", self.manager.weixin_windows)
        self.assertIsNone(self.manager.get_icon_position("send_button"))
        self.assertEqual(self.manager.MSG_HEIGHT, 0)
        self.assertFalse(self.manager.window_aligned)
        self.assertEqual(self.manager.last_window_state, "layout_unavailable")

class LayoutReadinessTest(WindowManagerFixture):
    def test_blank_and_nearly_flat_frames_have_no_boundary(self):
        for contrast in (0, 1):
            with self.subTest(contrast=contrast):
                frame = Image.new("RGB", (1008, 1820), (240, 240, 240))
                ImageDraw.Draw(frame).rectangle(
                    (301, 1679, 1007, 1819), fill=(240 - contrast,) * 3,
                )
                self.assertIsNone(self.manager._find_horizontal_boundary(
                    frame, 1460, 1740, 311, 998,
                ))
                self.manager.image_processor.take_screenshot.return_value = frame
                self.assertFalse(self.manager._init_window_part_size())
                self.assert_unready()

    def test_partially_rendered_frames_are_not_cached(self):
        for title, editor in ((True, False), (False, True)):
            with self.subTest(title=title, editor=editor):
                self.manager.image_processor.take_screenshot.return_value = self.frame(
                    title=title, editor=editor,
                )
                self.assertFalse(self.manager._init_window_part_size())
                self.assert_unready()

    def test_input_height_limits_reject_reported_bad_layout(self):
        for height in (81, 99, 100, 141, 200, 201, 360):
            with self.subTest(height=height):
                self.manager.image_processor.take_screenshot.return_value = self.frame(height)
                self.assertEqual(self.manager._init_window_part_size(), 100 <= height <= 200)
                if 100 <= height <= 200:
                    self.assertEqual(self.manager.MSG_TOP_Y, 80)
                    self.assertEqual(self.manager.MSG_HEIGHT, 1820 - height - 80)
                else:
                    self.assert_unready()

    def test_custom_input_height_limits(self):
        self.manager.rpa_config["layout"] = {"min_input_height": 220, "max_input_height": 300}
        self.manager.image_processor.take_screenshot.return_value = self.frame(260)
        self.assertTrue(self.manager._init_window_part_size())
        self.assertEqual(self.manager.MSG_HEIGHT, 1480)

    def test_visible_low_contrast_boundaries_still_work(self):
        # This separator is below the full-width detector's contrast threshold.
        self.manager.image_processor.take_screenshot.return_value = self.frame(contrast=3)
        self.assertTrue(self.manager._init_window_part_size())
        self.assertEqual(self.manager.MSG_TOP_Y + self.manager.MSG_HEIGHT, 1679)

    def test_startup_retries_after_blank_frame(self):
        self.mock_window()
        self.manager.image_processor.take_screenshot.side_effect = [
            Image.new("RGB", (1008, 1820), (240, 240, 240)), self.frame(),
        ]
        # The bot's existing initialization loop retries when this returns False.
        self.assertFalse(self.manager.init_chat_window())
        self.assert_unready()
        self.assertTrue(self.manager.init_chat_window())
        self.assertEqual(self.manager.current_window["MSG_HEIGHT"], 1599)
        self.assertEqual(self.manager.last_window_state, "ready")

    def test_failed_relayout_clears_previous_geometry_and_session(self):
        self.mock_window()
        self.manager.image_processor.take_screenshot.return_value = self.frame()
        self.assertTrue(self.manager.init_chat_window())
        self.manager.last_switch_session = self.manager.current_session_name = "old chat"
        self.manager.image_processor.take_screenshot.return_value = None
        self.assertFalse(self.manager.init_chat_window())
        self.assert_unready()
        self.assertIsNone(self.manager.last_switch_session)
        self.assertEqual(self.manager.current_session_name, "")

    def test_preflight_retries_invalid_cache_without_window_resize(self):
        self.mock_window()
        self.manager.MSG_TOP_X, self.manager.MSG_TOP_Y = 301, 52
        self.manager.MSG_WIDTH, self.manager.MSG_HEIGHT = 707, 1408
        self.manager._set_current_layout(1008, 1820)
        self.manager.image_processor.take_screenshot.side_effect = [
            Image.new("RGB", (1008, 1820), (240, 240, 240)), self.frame(),
        ]
        self.assertFalse(self.manager.ensure_action_ready())
        self.assert_unready()
        self.assertTrue(self.manager.ensure_action_ready())
        self.assertEqual(self.manager.current_window["MSG_HEIGHT"], 1599)
        self.manager._resize_wechat_window.assert_not_called()
        # Once valid, unchanged geometry continues using the cached layout.
        self.assertTrue(self.manager.ensure_action_ready())
        self.assertEqual(self.manager.image_processor.take_screenshot.call_count, 2)


class StartupSessionTest(WindowManagerFixture):
    def setUp(self):
        super().setUp()
        self.manager.rpa_config["layout"]["startup_session"] = "文件传输助手"
        self.manager.ocr_processor.process_image.return_value = [
            {"label": "搜索", "pixel_bbox": [10, 10, 50, 30]},
            {"label": "文件传输助手", "pixel_bbox": [45, 100, 150, 120]},
        ]

    def welcome(self):
        image = self.frame(title=False, editor=False)
        draw = ImageDraw.Draw(image)
        draw.ellipse((620, 900, 665, 942), fill=(210, 210, 210))
        draw.ellipse((645, 920, 690, 958), fill=(210, 210, 210))
        return image

    def test_welcome_requires_watermark_and_populated_session_list(self):
        self.assertEqual(self.manager._empty_startup_pane(self.welcome()), 301)
        self.assertIsNone(self.manager._empty_startup_pane(self.frame(title=False, editor=False)))
        self.assertIsNone(self.manager._empty_startup_pane(self.frame()))
        popup = self.welcome()
        ImageDraw.Draw(popup).rectangle((500, 700, 800, 1100), fill=(150, 150, 150))
        self.assertIsNone(self.manager._empty_startup_pane(popup))
        self.manager.ocr_processor.process_image.return_value = []
        self.assertIsNone(self.manager._empty_startup_pane(self.welcome()))

    def test_welcome_search_placeholder_tolerates_magnifier_ocr(self):
        for label in ("Q搜索", "Q 搜索", "Q Search"):
            with self.subTest(label=label):
                self.manager.ocr_processor.process_image.return_value[0]["label"] = label
                self.assertEqual(self.manager._empty_startup_pane(self.welcome()), 301)
        self.manager.ocr_processor.process_image.return_value[0]["label"] = "Something else"
        self.assertIsNone(self.manager._empty_startup_pane(self.welcome()))

    def test_startup_opens_only_after_two_empty_observations_and_verifies(self):
        self.mock_window()
        self.manager.image_processor.take_screenshot.return_value = self.welcome()
        with (
            patch.object(self.manager, "_init_window_part_size", side_effect=[False, False, True]),
            patch.object(self.manager, "_open_startup_session", return_value=True) as open_session,
            patch.object(self.manager, "_startup_title_matches", return_value=True) as title,
        ):
            self.assertFalse(self.manager.init_chat_window())
            open_session.assert_not_called()
            self.assertTrue(self.manager.init_chat_window())
            open_session.assert_called_once_with("文件传输助手")
            title.assert_called_once_with("文件传输助手")
            self.assertTrue(self.manager._startup_complete)
            self.assertEqual(self.manager.last_window_state, "ready")

    def test_existing_valid_chat_does_not_trigger_navigation(self):
        self.mock_window()
        self.manager.image_processor.take_screenshot.return_value = self.frame()
        with patch.object(self.manager, "_open_startup_session") as open_session:
            self.assertTrue(self.manager.init_chat_window())
            open_session.assert_not_called()
            self.manager.ocr_processor.process_image.assert_not_called()

    def test_disabled_or_finished_startup_never_opens_session(self):
        for disabled in (False, True):
            with self.subTest(disabled=disabled):
                self.manager._startup_complete = not disabled
                self.manager.rpa_config["layout"]["startup_session"] = "" if disabled else "文件传输助手"
                with (
                    patch.object(self.manager, "_init_window_part_size", return_value=False),
                    patch.object(self.manager, "_open_startup_session") as open_session,
                ):
                    self.assertFalse(self.manager._init_startup_layout())
                    open_session.assert_not_called()
        self.manager.image_processor.take_screenshot.assert_not_called()

    def test_failed_search_backs_off_and_partial_frame_resets_confirmation(self):
        now = [100.0]
        self.manager.image_processor.take_screenshot.return_value = self.welcome()
        with (
            patch.object(self.manager, "_init_window_part_size", return_value=False),
            patch.object(self.manager, "_open_startup_session", return_value=False) as open_session,
            patch("wechat_ai_bot.rpa.xfce_window_manager.time.monotonic", side_effect=lambda: now[0]),
        ):
            self.assertFalse(self.manager._init_startup_layout())
            self.manager.image_processor.take_screenshot.return_value = self.frame(title=False, editor=False)
            self.assertFalse(self.manager._init_startup_layout())
            self.manager.image_processor.take_screenshot.return_value = self.welcome()
            self.assertFalse(self.manager._init_startup_layout())
            open_session.assert_not_called()
            self.assertFalse(self.manager._init_startup_layout())
            self.assertEqual(open_session.call_count, 1)
            for _ in range(3):
                self.assertFalse(self.manager._init_startup_layout())
            self.assertEqual(open_session.call_count, 1)
            now[0] += 16
            self.assertFalse(self.manager._init_startup_layout())
            self.assertFalse(self.manager._init_startup_layout())
            self.assertEqual(open_session.call_count, 2)

    def test_pending_title_must_match_before_ready(self):
        self.mock_window()
        self.manager._startup_pending_session = "文件传输助手"
        self.manager.image_processor.take_screenshot.return_value = self.frame()
        self.manager.ocr_processor.process_image.return_value = [
            {"label": "Someone else", "pixel_bbox": [10, 10, 150, 30]},
        ]
        with patch.object(self.manager, "_open_startup_session") as open_session:
            self.assertFalse(self.manager.init_chat_window())
            self.assert_unready()
            self.manager.ocr_processor.process_image.return_value[0]["label"] = "文件传输助手"
            self.assertTrue(self.manager.init_chat_window())
            self.assertEqual(self.manager.MSG_HEIGHT, 1599)
            open_session.assert_not_called()

    def test_preflight_cannot_bypass_pending_title_verification(self):
        self.mock_window()
        self.manager._startup_pending_session = "文件传输助手"
        self.manager.image_processor.take_screenshot.return_value = self.frame()
        self.manager.ocr_processor.process_image.return_value = []
        self.assertFalse(self.manager.ensure_action_ready())
        self.assert_unready()
        self.manager.ocr_processor.process_image.return_value = [
            {"label": "文件传输助手", "pixel_bbox": [10, 10, 150, 30]},
        ]
        self.assertTrue(self.manager.ensure_action_ready())
        self.assertTrue(self.manager._startup_complete)

    def test_opened_session_waits_for_rendering_without_repeated_search(self):
        self.manager._startup_empty_frames = 1
        self.manager.image_processor.take_screenshot.return_value = self.welcome()
        def clicked(target):
            self.manager._startup_pending_session = target
            return True
        with (
            patch.object(self.manager, "_init_window_part_size", side_effect=[False, False, True]),
            patch.object(self.manager, "_open_startup_session", side_effect=clicked) as open_session,
            patch.object(self.manager, "_startup_title_matches", return_value=True),
            patch("wechat_ai_bot.rpa.xfce_window_manager.time.sleep"),
        ):
            self.assertFalse(self.manager._init_startup_layout())
            self.assertFalse(self.manager._startup_complete)
            self.assertTrue(self.manager._init_startup_layout())
            self.assertEqual(open_session.call_count, 1)

    def test_search_clicks_exact_local_result_without_enter_or_send(self):
        self.manager.window_id = "123"
        self.manager.image_processor.take_screenshot.return_value = Image.new("RGB", (330, 90))
        match = {"label": "文件传输助手", "pixel_bbox": [55, 40, 155, 60]}
        for rows, expected in (([], False), ([{**match, "label": "文件传输助手打开"}], False),
                               ([match, match], False), ([match], True)):
            with self.subTest(rows=rows):
                self.manager.ocr_processor.process_image.return_value = rows
                with (
                    patch.object(self.manager, "_active_window", return_value=types.SimpleNamespace(id="123")),
                    patch("wechat_ai_bot.rpa.xfce_window_manager.set_clipboard_text", return_value=True),
                    patch("wechat_ai_bot.rpa.xfce_window_manager.time.sleep"),
                    patch("wechat_ai_bot.rpa.xfce_window_manager._run", return_value=DummyCompletedProcess()) as run,
                ):
                    self.assertEqual(self.manager._open_startup_session("文件传输助手"), expected)
                    calls = [call.args for call in run.call_args_list]
                    self.assertNotIn(("xdotool", "key", "Return"), calls)
                    clicks = [call for call in calls if "click" in call]
                    self.assertEqual(len(clicks), int(expected))
                    if expected:
                        self.assertNotIn(("xdotool", "key", "Escape"), calls)
                        self.assertEqual(clicks[0], ("xdotool", "mousemove", "177", "135", "click", "1"))
                    else:
                        self.assertEqual(calls[-1], ("xdotool", "key", "Escape"))

    def test_search_does_not_type_into_another_active_window(self):
        self.manager.window_id = "123"
        with (
            patch.object(self.manager, "_active_window", return_value=types.SimpleNamespace(id="456")),
            patch("wechat_ai_bot.rpa.xfce_window_manager._run") as run,
        ):
            self.assertFalse(self.manager._open_startup_session("文件传输助手"))
            run.assert_not_called()

    def test_initialization_waits_for_ui_lock(self):
        from threading import Event, Thread
        entered = Event()
        with patch.object(self.manager, "_init_chat_window_locked", side_effect=lambda: entered.set()):
            with self.manager.ui_lock:
                worker = Thread(target=self.manager.init_chat_window)
                worker.start()
                self.assertFalse(entered.wait(0.05))
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertTrue(entered.is_set())


class XFCEWindowManagerTest(unittest.TestCase):
    def test_editor_focus_stays_above_toolbar_after_layout_shift(self):
        for left in (301, 321):
            manager = XFCEWindowManager(image_processor=object(), ocr_processor=object())
            manager.MSG_TOP_X, manager.MSG_TOP_Y = left, 80
            manager.MSG_WIDTH, manager.MSG_HEIGHT = 1008-left, 1599
            manager.ICON_CONFIGS["send_button"]["position"] = [892, 1770, 990, 1806]
            with (
                patch("wechat_ai_bot.rpa.xfce_window_manager._run", return_value=DummyCompletedProcess()) as run,
                patch("wechat_ai_bot.rpa.xfce_window_manager.time.sleep"),
            ):
                self.assertTrue(manager.activate_input_box())
            run.assert_called_once_with("xdotool", "mousemove", str(left+80), "1724", "click", "1")

    def test_missing_editor_geometry_fails_without_click(self):
        manager = XFCEWindowManager(image_processor=object(), ocr_processor=object())
        manager.ICON_CONFIGS["send_button"]["position"] = [892, 1770, 990, 1806]
        with patch("wechat_ai_bot.rpa.xfce_window_manager._run") as run:
            self.assertFalse(manager.activate_input_box())
        run.assert_not_called()

    def test_main_window_excludes_larger_browser_with_same_title(self):
        manager = XFCEWindowManager(image_processor=object(), ocr_processor=object())

        def fake_run(*args):
            if args[:2] == ("xdotool", "search"):
                return DummyCompletedProcess("2\n1\n")
            if args[:2] == ("xdotool", "getwindowname"):
                return DummyCompletedProcess("微信")
            return DummyCompletedProcess()

        with (
            patch("wechat_ai_bot.rpa.xfce_window_manager._run", side_effect=fake_run),
            patch.object(manager, "_managed_window_ids", return_value={"1", "2"}),
            patch.object(manager, "_window_geometry", side_effect=lambda wid: {
                "x": 0, "y": 0, "width": 1000, "height": 1800 if wid == "2" else 1200,
            }),
            patch.object(manager, "_is_main_process_window", side_effect=lambda wid: wid == "1"),
        ):
            self.assertEqual(manager._find_wechat_window(), "1")

    def test_main_process_window_rejects_wechat_browser(self):
        from pathlib import Path
        manager = XFCEWindowManager(image_processor=object(), ocr_processor=object())
        with (
            patch("wechat_ai_bot.rpa.xfce_window_manager._run", return_value=DummyCompletedProcess("977")),
            patch("wechat_ai_bot.rpa.xfce_window_manager.Path.resolve", return_value=Path("/opt/wechat/WeChatAppEx")),
        ):
            self.assertFalse(manager._is_main_process_window("2"))

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

    def test_room_sidebar_state_ignores_single_sidebar_marker(self):
        manager = XFCEWindowManager(
            image_processor=DummyImageProcessor(),
            ocr_processor=DummyOCRProcessor(labels=["消息免打扰", "Bread"]),
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
