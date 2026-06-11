import sys
import types
import unittest

from PIL import Image


if "mss" not in sys.modules:
    mss_module = types.ModuleType("mss")
    mss_module.mss = object
    mss_module.tools = types.ModuleType("mss.tools")
    sys.modules["mss"] = mss_module
    sys.modules["mss.tools"] = mss_module.tools

if "torch" not in sys.modules:
    torch_module = types.ModuleType("torch")
    torch_module.cuda = types.SimpleNamespace(is_available=lambda: False)
    sys.modules["torch"] = torch_module

if "ultralytics" not in sys.modules:
    ultralytics_module = types.ModuleType("ultralytics")
    ultralytics_module.YOLO = object
    sys.modules["ultralytics"] = ultralytics_module

from wechat_ai_bot.rpa.image_processor import ImageProcessor


class ImageProcessorTest(unittest.TestCase):
    def test_resolve_yolo_imgsz_auto_uses_image_shape_aligned_to_stride(self):
        processor = ImageProcessor()
        image = Image.new("RGB", (736, 1601), "white")

        self.assertEqual(processor.resolve_yolo_imgsz(image), [1632, 736])

    def test_resolve_yolo_imgsz_accepts_numeric_and_pair_values(self):
        processor = ImageProcessor()
        image = Image.new("RGB", (20, 20), "white")

        self.assertEqual(processor.resolve_yolo_imgsz(image, imgsz=961), 992)
        self.assertEqual(processor.resolve_yolo_imgsz(image, imgsz="1601,736"), [1632, 736])

    def test_detect_objects_records_resolved_imgsz(self):
        processor = ImageProcessor()
        calls = []

        class DummyYOLO:
            names = {}

            def __call__(self, image, imgsz, verbose=False):
                calls.append({"image": image, "imgsz": imgsz, "verbose": verbose})
                return []

        image = Image.new("RGB", (736, 1601), "white")
        processor.yolo = DummyYOLO()

        self.assertEqual(processor.detect_objects(image), [])
        self.assertEqual(calls[0]["imgsz"], [1632, 736])
        self.assertEqual(processor.last_yolo_imgsz, [1632, 736])


if __name__ == "__main__":
    unittest.main()
