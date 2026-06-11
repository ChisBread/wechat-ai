"""
图像处理模块。
提供截图、目标检测、绘制、像素颜色获取等功能。
"""

import logging
import math
import os
from pathlib import Path
import random
from typing import Dict, List, Optional, Tuple

import mss
import mss.tools
import torch
from PIL import Image, ImageDraw, ImageFont
from ultralytics import YOLO
from wechat_ai_bot.yolo.get_model_path import get_model_path


class ImageProcessor:
    """
    图像处理器。
    支持截图、目标检测、绘制检测框、像素颜色获取、生成带文字的图片等。
    """

    def __init__(self):
        """
        初始化 ImageProcessor，不加载模型，仅保存配置。
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        self.model_path = get_model_path("msg_rec.pt")
        self.yolo: Optional[YOLO] = None
        self.box_color_dict = {}
        self.yolo_stride = 32
        self.last_yolo_imgsz = None
        self._init_color_config()

    def setup(self):
        """
        加载 YOLO 模型。
        """
        self.logger.info(f"加载 YOLO 模型: {self.model_path}")
        try:
            device = "cuda" if torch.cuda.is_available() else "cpu"
            model = YOLO(self.model_path)
            model.to(device)
            self.yolo = model
            self.logger.info(f"YOLO 模型加载成功，设备: '{device}'。")
        except Exception as e:
            self.logger.error(f"YOLO 模型加载失败: {e}", exc_info=True)
            raise

    def _init_color_config(self):
        """
        初始化检测框颜色配置。
        """
        self.msg_types = [
            "avatar",
            "card",
            "name",
            "file",
            "image",
            "quote",
            "text",
            "video",
            "video_downloaded",
            "voice",
            "time",
            "pyq_image_add",
        ]
        for i in range(len(self.msg_types)):
            self.box_color_dict[self.msg_types[i]] = self._get_random_color()

    def _get_random_color(self) -> Tuple[int, int, int]:
        """
        生成随机颜色。
        Returns:
            Tuple[int, int, int]: RGB 颜色元组。
        """
        return (random.randint(0, 150), random.randint(0, 150), random.randint(0, 150))

    def resolve_yolo_imgsz(
        self,
        image: Image.Image,
        imgsz: int | str | list[int] | tuple[int, int] = "auto",
        stride: int | None = None,
    ) -> int | list[int]:
        stride = max(1, int(stride or self.yolo_stride or 32))
        config = imgsz
        if isinstance(config, str):
            value = config.strip().lower()
            if value == "auto":
                return [
                    self._ceil_to_stride(image.height, stride),
                    self._ceil_to_stride(image.width, stride),
                ]
            if "," in value:
                parts = [int(part.strip()) for part in value.split(",") if part.strip()]
                if len(parts) == 2:
                    return [
                        self._ceil_to_stride(parts[0], stride),
                        self._ceil_to_stride(parts[1], stride),
                    ]
            return self._ceil_to_stride(int(value), stride)
        if isinstance(config, (list, tuple)) and len(config) == 2:
            return [
                self._ceil_to_stride(int(config[0]), stride),
                self._ceil_to_stride(int(config[1]), stride),
            ]
        return self._ceil_to_stride(int(config or 960), stride)

    def _ceil_to_stride(self, value: int, stride: int) -> int:
        return max(stride, int(math.ceil(value / stride) * stride))

    def detect_objects(
        self,
        image: Image.Image,
        imgsz: int | str | list[int] | tuple[int, int] = "auto",
    ) -> List[Dict]:
        """
        检测图像中的对象。
        Args:
            image (Image.Image): 输入图片。
        Returns:
            List[Dict]: 检测结果列表。
        """
        if self.yolo is None:
            self.logger.error("YOLO 模型未加载")
            return []
        try:
            resolved_imgsz = self.resolve_yolo_imgsz(image, imgsz=imgsz)
            self.last_yolo_imgsz = resolved_imgsz
            self.logger.debug(
                "YOLO input image size: %sx%s, imgsz=%s",
                image.width,
                image.height,
                resolved_imgsz,
            )
            results = self.yolo(image, imgsz=resolved_imgsz, verbose=False)
            detections = []
            for result in results:
                boxes = result.boxes
                for box in boxes:
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    conf = box.conf[0].item()
                    cls = box.cls[0].item()
                    cls_name = self.yolo.names[int(cls)]
                    detections.append(
                        {
                            "label": cls_name,
                            "pixel_bbox": [x1, y1, x2, y2],
                            "content": f"{cls_name} {round(conf, 3)}",
                            "class": cls_name,
                            "confidence": round(conf, 3),
                            "bbox": [x1, y1, x2, y2],
                        }
                    )
            return detections
        except Exception as e:
            self.logger.error(f"YOLO 检测出错: {str(e)}")
            return []

    def take_screenshot(
        self, region: Tuple[int, int, int, int], save_path: Optional[str] = None
    ) -> Optional[Image.Image]:
        """
        Take screenshot (X11 via mss, Wayland/grim fallback).
        Args:
            region (Tuple[int, int, int, int]): Region (left, top, width, height).
            save_path (Optional[str]): Save path.
        Returns:
            Optional[Image.Image]: Screenshot image.
        """
        if region is None:
            raise ValueError("Region cannot be None")
        left, top, width, height = [int(v) for v in region]
        if width <= 0 or height <= 0:
            raise ValueError(f"Invalid screenshot region: {region}")

        try:
            with mss.mss() as sct:
                monitor = {
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                }
                screenshot = sct.grab(monitor)
                img = Image.frombytes(
                    "RGB", screenshot.size, screenshot.bgra, "raw", "BGRX"
                )
                if save_path:
                    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
                    img.save(save_path)
                return img
        except Exception as x11_error:
            try:
                import subprocess
                import tempfile
                tmp = tempfile.mktemp(suffix=".png")
                env = {**os.environ, "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", "/config/.XDG")}
                subprocess.run(
                    ["grim", "-g", f"{left},{top} {width}x{height}", tmp],
                    timeout=5, capture_output=True, check=True, env=env
                )
                img = Image.open(tmp)
                os.unlink(tmp)
                if save_path:
                    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
                    img.save(save_path)
                return img
            except Exception as e:
                self.logger.error(f"Screenshot error: {x11_error}; grim fallback: {e}")
                return None

    def draw_boxes_on_screen(
        self,
        screenshot: Image.Image,
        parsed_content: List[Dict],
        output_path: str = None,
        start: Tuple[int, int] = None,
    ) -> Image.Image:
        """
        在截图上绘制检测到的框。
        Args:
            screenshot (Image.Image): 输入截图。
            parsed_content (List[Dict]): 检测结果。
            output_path (str): 输出路径。
            start (Tuple[int, int]): 坐标偏移。
        Returns:
            Image.Image: 绘制后的图片。
        """
        draw = ImageDraw.Draw(screenshot)
        try:
            font_paths = [
                # Linux fonts (check first in container)
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
                # Windows fonts (fallback)
                "C:/Windows/Fonts/simhei.ttf",
                "C:/Windows/Fonts/simsun.ttc",
                "C:/Windows/Fonts/msyh.ttc",
                "C:/Windows/Fonts/msyhbd.ttc",
            ]
            font = None
            for font_path in font_paths:
                try:
                    font = ImageFont.truetype(font_path, 18)
                    break
                except:
                    continue
            if font is None:
                raise Exception("未找到合适的中文字体")
        except Exception as e:
            self.logger.error(f"加载字体出错: {str(e)}")
            font = ImageFont.load_default()
        for item in parsed_content:
            if "pixel_bbox" in item:
                bbox = item["pixel_bbox"].copy()
                color = self.box_color_dict.get(item.get("label", "default"), None)
                if not color:
                    color = self._get_random_color()
                if start:
                    bbox[0] += start[0]
                    bbox[1] += start[1]
                    bbox[2] += start[0]
                    bbox[3] += start[1]
                draw.rectangle(
                    [(bbox[0], bbox[1]), (bbox[2], bbox[3])], outline=color, width=4
                )
                text = f"{item.get('content', '')}"
                draw.text((bbox[0], bbox[1] - 20), text, fill=color, font=font)
        if output_path:
            screenshot.save(output_path)
        return screenshot

    def get_pixel_color(self, x: int, y: int) -> Tuple[int, int, int]:
        """
        获取指定屏幕坐标的像素颜色。
        Args:
            x (int): 横坐标。
            y (int): 纵坐标。
        Returns:
            Tuple[int, int, int]: RGB 颜色。
        """
        monitor = {"top": y, "left": x, "width": 1, "height": 1}
        with mss.mss() as sct:
            sct_img = sct.grab(monitor)
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            pixel_color = img.getpixel((0, 0))
            return pixel_color

    def generate_image(
        self,
        text="自动查找字体",
        width=600,
        height=300,
        font_size=60,
        output_filename="generated_image.png",
    ):
        """
        生成带有指定文字的随机噪点背景图片。
        Args:
            text (str): 文字内容。
            width (int): 图片宽度。
            height (int): 图片高度。
            font_size (int): 字体大小。
            output_filename (str): 输出文件名。
        Returns:
            str: 图片文件路径。
        """
        font_paths_to_check = [
            # Linux fonts (check first)
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            # macOS
            "/System/Library/Fonts/PingFang.ttc",
            # Windows (fallback)
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/simsun.ttc",
        ]
        font_path = None
        for path in font_paths_to_check:
            if os.path.exists(path):
                font_path = path
                break
        image = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(image)
        for x in range(width):
            for y in range(height):
                draw.point(
                    (x, y),
                    fill=(
                        random.randint(0, 255),
                        random.randint(0, 255),
                        random.randint(0, 255),
                    ),
                )
        font = None
        if font_path:
            try:
                font = ImageFont.truetype(font_path, font_size, index=0)
            except Exception as e:
                font = ImageFont.load_default()
        else:
            font = ImageFont.load_default()
        text_bbox = draw.textbbox((0, 0), text, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        text_x = (width - text_width) / 2
        text_y = (height - text_height) / 2 - text_bbox[1]
        shadow_offset = 2
        shadow_color = (0, 0, 0)
        draw.text(
            (text_x + shadow_offset, text_y + shadow_offset),
            text,
            font=font,
            fill=shadow_color,
        )
        text_color = (255, 255, 255)
        draw.text((text_x, text_y), text, font=font, fill=text_color)
        image.save(output_filename)
        return output_filename
