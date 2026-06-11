import sys
import types
import unittest
from unittest.mock import patch

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
        return object()


class DummyOCRProcessor:
    def process_image(self, **_kwargs):
        return [
            {"pixel_bbox": [10, 20, 120, 48], "label": "Bread", "confidence": 0.99}
        ]


class MessageSenderMentionTest(unittest.TestCase):
    def test_mention_user_uses_at_key_and_plain_search_name(self):
        sender = MessageSender(DummyWindowManager())
        pressed = []
        hotkeys = []
        clicks = []
        clipboards = []

        with (
            patch("wechat_ai_bot.rpa.message_sender.time.sleep", lambda _seconds: None),
            patch(
                "wechat_ai_bot.rpa.message_sender.set_clipboard_text",
                lambda text: clipboards.append(text) or True,
            ),
            patch(
                "wechat_ai_bot.rpa.message_sender.pyautogui.press",
                lambda key: pressed.append(key),
            ),
            patch(
                "wechat_ai_bot.rpa.message_sender.pyautogui.hotkey",
                lambda *keys: hotkeys.append(keys),
            ),
            patch(
                "wechat_ai_bot.rpa.message_sender.pyautogui.click",
                lambda x, y: clicks.append((x, y)),
            ),
        ):
            ok = sender.mention_user("@Bread extra")

        self.assertTrue(ok)
        self.assertEqual(pressed, ["at", "space"])
        self.assertEqual(hotkeys, [("ctrl", "v")])
        self.assertEqual(clipboards, ["Bread"])
        self.assertEqual(clicks, [(265, 494)])

    def test_mention_candidate_region_stays_near_input_box(self):
        sender = MessageSender(DummyWindowManager())

        region = sender._mention_candidate_region([720, 920, 780, 960])

        self.assertEqual(region, [200, 460, 600, 260])

    def test_mention_candidates_prefer_lower_popup_candidate(self):
        sender = MessageSender(DummyWindowManager())
        results = [
            {"pixel_bbox": [10, 20, 120, 48], "label": "Bread", "confidence": 0.99},
            {"pixel_bbox": [10, 210, 120, 238], "label": "Bread", "confidence": 0.99},
        ]

        candidates = sender._mention_candidates("Bread", results)

        self.assertEqual(candidates[0]["pixel_bbox"], [10, 210, 120, 238])


if __name__ == "__main__":
    unittest.main()
