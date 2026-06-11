import sys
import types
import unittest
from importlib.util import find_spec
from difflib import SequenceMatcher
from unittest.mock import patch


def _module_exists(module_name):
    try:
        return find_spec(module_name) is not None
    except (ImportError, AttributeError, ValueError):
        return module_name in sys.modules


if not _module_exists("cv2"):
    sys.modules.setdefault("cv2", types.ModuleType("cv2"))

if not _module_exists("mss"):
    mss = types.ModuleType("mss")
    mss.mss = object
    sys.modules.setdefault("mss", mss)

if not _module_exists("numpy"):
    numpy = types.ModuleType("numpy")
    numpy.array = lambda value: value
    numpy.uint8 = object
    sys.modules.setdefault("numpy", numpy)

if not _module_exists("fuzzywuzzy"):
    fuzzywuzzy = types.ModuleType("fuzzywuzzy")
    fuzz = types.SimpleNamespace(
        ratio=lambda left, right: int(
            SequenceMatcher(None, str(left), str(right)).ratio() * 100
        )
    )

    def _extract(query, choices, processor=None, limit=None):
        items = []
        for choice in choices:
            processed = processor(choice) if processor else choice
            score = fuzz.ratio(query, processed)
            items.append((choice, score))
        items.sort(key=lambda item: item[1], reverse=True)
        return items if limit is None else items[:limit]

    process = types.SimpleNamespace(extract=_extract)
    fuzzywuzzy.fuzz = fuzz
    fuzzywuzzy.process = process
    sys.modules.setdefault("fuzzywuzzy", fuzzywuzzy)
    sys.modules.setdefault("fuzzywuzzy.fuzz", fuzz)
    sys.modules.setdefault("fuzzywuzzy.process", process)

import wechat_ai_bot.rpa.ui_helper as ui_helper_module

UIInteractionHelper = ui_helper_module.UIInteractionHelper


class DummyImageProcessor:
    def take_screenshot(self, region, save_path=None):
        return object()


class DummyOCRProcessor:
    def __init__(self, results):
        self.results = results

    def process_image(self, image):
        return list(self.results)


class DummyWindowManager:
    action_delay = 0


class DummyLogger:
    def getChild(self, _name):
        return self

    def warning(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass


class DummyController:
    def __init__(self, ocr_results):
        self.image_processor = DummyImageProcessor()
        self.ocr_processor = DummyOCRProcessor(ocr_results)
        self.window_manager = DummyWindowManager()
        self.logger = DummyLogger()


class UIInteractionHelperTest(unittest.TestCase):
    def test_text_candidate_clicks_exact_short_button_not_long_title(self):
        helper = UIInteractionHelper(
            DummyController(
                [
                    {
                        "pixel_bbox": [273.0, 220.0, 426.0, 239.0],
                        "label": "修改我在本群的昵称？",
                        "confidence": 0.95,
                    },
                    {
                        "pixel_bbox": [276.0, 278.0, 311.0, 300.0],
                        "label": "修改",
                        "confidence": 0.99,
                    },
                ]
            )
        )

        with (
            patch.object(ui_helper_module, "human_like_mouse_move") as move,
            patch.object(ui_helper_module.pyautogui, "click") as click,
        ):
            bbox = helper.find_and_click_text_candidate(
                texts=("修改",),
                region=[151, 650, 705, 520],
                fuzzy=76,
            )

        self.assertEqual(bbox, [427, 928, 462, 950])
        move.assert_called_once_with(target_x=444, target_y=939)
        click.assert_called_once_with(444, 939)

    def test_text_candidate_rejects_short_text_inside_long_title(self):
        helper = UIInteractionHelper(
            DummyController(
                [
                    {
                        "pixel_bbox": [273.0, 220.0, 426.0, 239.0],
                        "label": "修改我在本群的昵称？",
                        "confidence": 0.95,
                    },
                ]
            )
        )

        with (
            patch.object(ui_helper_module, "human_like_mouse_move") as move,
            patch.object(ui_helper_module.pyautogui, "click") as click,
        ):
            bbox = helper.find_and_click_text_candidate(
                texts=("修改",),
                region=[151, 650, 705, 520],
                fuzzy=76,
            )

        self.assertIsNone(bbox)
        move.assert_not_called()
        click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
