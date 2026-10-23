import sys
import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

levenshtein = types.ModuleType("Levenshtein")
levenshtein.ratio = lambda *_args, **_kwargs: 1.0
sys.modules.setdefault("Levenshtein", levenshtein)

from wechat_ai_bot.rpa.message_sender import MessageSender


class DummyWindowManager:
    action_delay = 0
    MSG_TOP_X = 200
    MSG_TOP_Y = 50
    MSG_WIDTH = 600
    MSG_HEIGHT = 650

    def __init__(self):
        self.closed = False
        self.sidebar_closed = False
        self.image_processor = DummyImageProcessor()
        self.ocr_processor = DummyOCRProcessor()

    def activate_input_box(self):
        return True

    def get_icon_position(self, _icon_name):
        return [720, 920, 780, 960]

    def switch_session(self, _target):
        return True

    def close_all_windows(self):
        self.closed = True

    def open_close_sidebar(self, close=False):
        self.sidebar_closed = close


class DummyImageProcessor:
    def take_screenshot(self, **_kwargs):
        return Image.new("RGB", (800, 520), "white")


class DummyOCRProcessor:
    def process_image(self, **_kwargs):
        return [
            {"pixel_bbox": [10, 400, 120, 428], "label": "Bread", "confidence": 0.99}
        ]


class MessageSenderMentionTest(unittest.TestCase):
    def prepare_popup(self, labels, copied_text):
        window = DummyWindowManager()
        before = Image.new("RGB", (800, 520), "white")
        active = before.copy()
        draw = ImageDraw.Draw(active)
        for item in labels:
            draw.rectangle(item["pixel_bbox"], fill="gray")
        window.image_processor.take_screenshot = Mock(side_effect=[before, active])
        window.ocr_processor.process_image = Mock(return_value=labels)
        for target, kwargs in [
            ("time.sleep", {}),
            ("set_clipboard_text", {"return_value": True}),
            ("get_clipboard_text", {"return_value": copied_text}),
            ("pyautogui.press", {}),
            ("pyautogui.hotkey", {}),
            ("pyautogui.click", {}),
        ]:
            patcher = patch("wechat_ai_bot.rpa.message_sender." + target, **kwargs)
            setattr(self, target.split(".")[-1], patcher.start())
            self.addCleanup(patcher.stop)
        return MessageSender(window), before, active

    def test_y_clicks_second_exact_candidate_instead_of_enter_on_yang(self):
        sender, _, _ = self.prepare_popup([
            {"pixel_bbox": [20, 410, 90, 435], "label": "Yang"},
            {"pixel_bbox": [20, 455, 35, 480], "label": "y"},
            {"pixel_bbox": [20, 300, 150, 325], "label": "Kazanskaya"},
        ], "\u2005@y\u2005")

        self.assertTrue(sender.mention_user("y"))
        self.click.assert_called_once_with(27, 647)
        self.assertNotIn(unittest.mock.call("enter"), self.press.call_args_list)
        self.assertEqual(self.press.call_args_list[-1], unittest.mock.call("Right"))

    def test_full_name_including_spaces_is_used(self):
        sender, _, _ = self.prepare_popup([
            {"pixel_bbox": [20, 455, 150, 480], "label": "Bread extra"},
        ], "@Bread extra ")
        self.assertTrue(sender.mention_user("@Bread extra"))
        self.set_clipboard_text.assert_any_call("Bread extra")

    def test_missing_or_duplicate_exact_candidates_fail_without_clicking(self):
        for labels in [
            [{"pixel_bbox": [20, 410, 90, 435], "label": "Yang"}],
            [{"pixel_bbox": [20, 410, 90, 435], "label": "Kazanskaya"}],
            [{"pixel_bbox": [20, 410, 35, 435], "label": "y"},
             {"pixel_bbox": [20, 455, 35, 480], "label": "y"}],
        ]:
            with self.subTest(labels=labels):
                sender, _, _ = self.prepare_popup(labels, "@y ")
                self.assertFalse(sender.mention_user("y"))
                self.click.assert_not_called()
                self.assertNotIn(unittest.mock.call("esc"), self.press.call_args_list)
                self.assertEqual(self.press.call_args_list[-1], unittest.mock.call("backspace"))

    def test_unchanged_history_is_not_a_candidate(self):
        sender, before, active = self.prepare_popup([
            {"pixel_bbox": [20, 455, 35, 480], "label": "y"},
        ], "@y ")
        before.paste(active)
        self.assertFalse(sender.mention_user("y"))
        self.click.assert_not_called()

    def test_verification_rejects_wrong_name_plain_query_and_stale_clipboard(self):
        for copied in ["@Yang ", "@y", "@Y ", "@y hello", "", "@y\n", "@y@Yang "]:
            with self.subTest(copied=copied):
                sender, _, _ = self.prepare_popup([
                    {"pixel_bbox": [20, 455, 35, 480], "label": "y"},
                ], copied)
                self.assertFalse(sender.mention_user("y"))
                self.assertNotIn(unittest.mock.call("esc"), self.press.call_args_list)
                self.assertEqual(self.press.call_args_list[-1], unittest.mock.call("backspace"))

    def test_failed_click_ocr_or_capture_cleans_up_without_enter(self):
        for failure in ["click", "ocr", "screenshot", "clipboard"]:
            with self.subTest(failure=failure):
                sender, _, _ = self.prepare_popup([
                    {"pixel_bbox": [20, 455, 35, 480], "label": "y"},
                ], "@y ")
                if failure == "click":
                    self.click.side_effect = RuntimeError("click failed")
                elif failure == "ocr":
                    sender.window_manager.ocr_processor.process_image.side_effect = RuntimeError("OCR failed")
                elif failure == "screenshot":
                    sender.window_manager.image_processor.take_screenshot.side_effect = [None]
                else:
                    self.set_clipboard_text.return_value = False
                self.assertFalse(sender.mention_user("y"))
                self.assertNotIn(unittest.mock.call("enter"), self.press.call_args_list)
                self.assertNotIn(unittest.mock.call("esc"), self.press.call_args_list)

    def test_failed_input_activation_never_types_query(self):
        sender, _, _ = self.prepare_popup([], "")
        sender.window_manager.activate_input_box = Mock(return_value=False)
        self.assertFalse(sender.mention_user("y"))
        self.hotkey.assert_not_called()

    def test_short_name_uses_zoomed_changed_popup_when_native_ocr_misses_it(self):
        sender, before, active = self.prepare_popup([], "@y ")
        ImageDraw.Draw(active).rectangle([100, 300, 250, 500], fill="gray")
        for frame in (before, active):
            ImageDraw.Draw(frame).rectangle([20, 20, 30, 40], fill="black")
        sender.window_manager.ocr_processor.process_image.side_effect = [
            [{"label": "Yang", "pixel_bbox": [120, 320, 160, 340]},
             {"label": "y", "pixel_bbox": [20, 20, 30, 40]}],
            [{"label": "y", "pixel_bbox": [60, 300, 90, 360]}],
        ]
        self.assertTrue(sender.mention_user("y"))
        self.click.assert_called_once_with(125, 590)
        image = sender.window_manager.ocr_processor.process_image.call_args.kwargs["image"]
        self.assertEqual(image.size, (453, 603))

    def test_wrong_or_missing_draft_never_clicks_send(self):
        for actual in (None, "", "different text"):
            sender, _, _ = self.prepare_popup([], "")
            with patch.object(sender, "_read_input_text", return_value=actual):
                self.assertFalse(sender.send_message("hello"))
            self.click.assert_not_called()

    def test_disabled_send_button_is_not_clicked_even_when_draft_matches(self):
        sender, _, _ = self.prepare_popup([], "")
        with (
            patch.object(sender, "_read_input_text", return_value="hello"),
            patch("wechat_ai_bot.rpa.message_sender.enabled_send_button", return_value=None),
        ):
            self.assertFalse(sender.send_message("hello"))
        self.click.assert_not_called()

    def test_send_message_can_skip_input_activation_after_mention(self):
        window = DummyWindowManager()
        sender = MessageSender(window)
        window.activated = 0

        def activate_input_box():
            window.activated += 1
            return True

        window.activate_input_box = activate_input_box

        with (
            patch("wechat_ai_bot.rpa.message_sender.time.sleep", lambda _seconds: None),
            patch("wechat_ai_bot.rpa.message_sender.set_clipboard_text", lambda _text: True),
            patch("wechat_ai_bot.rpa.message_sender.pyautogui.hotkey"),
            patch("wechat_ai_bot.rpa.message_sender.pyautogui.press"),
            patch("wechat_ai_bot.rpa.message_sender.pyautogui.click"),
        ):
            with (
                patch.object(sender, "_read_input_text", side_effect=["@Bread ", "@Bread hello"]),
                patch("wechat_ai_bot.rpa.message_sender.enabled_send_button", return_value=[720, 920, 780, 960]),
            ):
                ok = sender.send_message("hello", clear_input_box=False, activate_input_box=False)

        self.assertTrue(ok)
        self.assertEqual(window.activated, 0)

    def test_mention_candidate_region_stays_near_input_box(self):
        sender = MessageSender(DummyWindowManager())

        region = sender._mention_candidate_region([720, 920, 780, 960])

        self.assertEqual(region, [0, 180, 800, 520])

    def test_mention_candidates_ignore_unselected_message_text(self):
        sender = MessageSender(DummyWindowManager())
        results = [
            {"pixel_bbox": [380, 35, 450, 51], "label": "@Bread1", "confidence": 0.99},
            {"pixel_bbox": [10, 410, 120, 438], "label": "Bread", "confidence": 0.99},
            {"pixel_bbox": [18, 452, 72, 471], "label": "@Bread", "confidence": 0.99},
        ]

        candidates = sender._mention_candidates("Bread", results, region_width=600, region_height=480)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["pixel_bbox"], [10, 410, 120, 438])

    def test_mention_candidates_match_real_probe_shape(self):
        sender = MessageSender(DummyWindowManager())
        results = [
            {"pixel_bbox": [590, 21, 652, 39], "label": "@Bread2", "confidence": 0.99},
            {"pixel_bbox": [590, 119, 652, 137], "label": "@Bread3", "confidence": 0.99},
            {"pixel_bbox": [23, 185, 65, 200], "label": "Bread", "confidence": 0.99},
            {"pixel_bbox": [18, 223, 72, 242], "label": "@Bread", "confidence": 0.99},
        ]

        candidates = sender._mention_candidates("Bread", results, region_width=736, region_height=270)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["pixel_bbox"], [23, 185, 65, 200])

    def test_candidate_matching_preserves_case_and_spaces_and_rejects_bad_boxes(self):
        sender = MessageSender(DummyWindowManager())
        for label, bbox in [
            ("Y", [20, 455, 35, 480]),
            ("y extra", [20, 455, 35, 480]),
            ("y", [-1, 455, 35, 480]),
            ("y", [20, 455, 35, 600]),
            ("y", [20, 455, 20, 480]),
            ("y", [20, 455]),
            ("y", ["bad", 455, 35, 480]),
            ("y", [float("nan"), 455, 35, 480]),
        ]:
            with self.subTest(label=label, bbox=bbox):
                self.assertEqual(sender._mention_candidates(
                    "y", [{"label": label, "pixel_bbox": bbox}], 600, 520,
                ), [])

    def test_candidates_near_top_of_long_popup_are_not_discarded(self):
        sender = MessageSender(DummyWindowManager())
        row = {"label": "y", "pixel_bbox": [20, 10, 35, 35]}
        self.assertEqual(sender._mention_candidates("y", [row], 600, 520), [row])


if __name__ == "__main__":
    unittest.main()
