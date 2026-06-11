"""
Visual Message Service for WeChat-AI Bot.
Reads messages from the WeChat chat window via OCR + YOLO vision.
This replaces the DatabaseService (Windows-only .pyd) until we
implement direct SQLCipher DB access on Linux.

Architecture:
  Screenshot message area → YOLO detect message bubbles → OCR text → deduplicate → emit
"""

import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

from PIL import Image, ImageFilter, ImageStat

from wechat_ai_bot.rpa.image_processor import ImageProcessor
from wechat_ai_bot.rpa.ocr_processor import OCRProcessor


class VisualMessageService:
    """
    Polls the chat window visually to detect new messages via OCR + YOLO.

    Limitations:
    - Only reads messages in the currently visible chat window
    - Polling latency (configurable, default 2 seconds)
    - OCR accuracy depends on font rendering
    - Cannot read scrolled-off messages
    """

    def __init__(
        self,
        window_manager: Any,
        image_processor: ImageProcessor,
        ocr_processor: OCRProcessor,
        message_queue,
        poll_interval: float = 2.0,
        max_message_age: int = 300,
        dedup_cache_size: int = 100,
        yolo_imgsz: int | str | list[int] | tuple[int, int] = "auto",
        yolo_stride: int = 32,
    ):
        self.logger = logging.getLogger(__name__)
        self.window_manager = window_manager
        self.image_processor = image_processor
        self.ocr_processor = ocr_processor
        self.message_queue = message_queue
        self.poll_interval = poll_interval
        self.max_message_age = max_message_age
        self.dedup_cache_size = dedup_cache_size
        self.yolo_imgsz = yolo_imgsz
        self.yolo_stride = max(1, int(yolo_stride or 32))
        self._last_yolo_imgsz = None
        self.yolo_message_labels = {"text"}
        self.yolo_context_labels = {"avatar", "name", "quote", "time"}
        self.dedup_region_bucket = 48

        self.is_running = False
        self._thread: Optional[threading.Thread] = None

        # Deduplication: LRU cache of seen message hashes
        self._seen_hashes: OrderedDict = OrderedDict()
        # Track the last message text per contact for change detection
        self._last_messages: dict = {}
        self._seen_visual_hashes: OrderedDict = OrderedDict()
        self._primed = False
        self._current_session_id = ""
        self._current_session_name = ""
        self._last_title_image_hash = ""
        self._last_title_text = ""
        self._context_text_cache: OrderedDict = OrderedDict()

    def start(self):
        """Start the polling thread."""
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="VisualMessagePoller"
        )
        self._thread.start()
        self.logger.info(
            "VisualMessageService started (poll interval: %ss, yolo_imgsz=%s)",
            self.poll_interval,
            self.yolo_imgsz,
        )

    def stop(self):
        """Stop the polling thread."""
        self.is_running = False
        if self._thread:
            self._thread.join(timeout=5)
        self.logger.info("VisualMessageService stopped")

    def _poll_loop(self):
        """Main polling loop."""
        consecutive_failures = 0
        while self.is_running:
            try:
                messages = self._read_visible_messages()
                if messages:
                    consecutive_failures = 0
                    for msg in messages:
                        self.message_queue.put(msg)
                        self.logger.debug(f"Visual message: {msg}")
                else:
                    consecutive_failures += 1
                    if consecutive_failures % 10 == 0:
                        self.logger.debug(
                            "No new messages detected (%s polls)",
                            consecutive_failures,
                        )
            except Exception as e:
                self.logger.error(f"Error in visual message poll: {e}", exc_info=True)
                consecutive_failures += 1

            time.sleep(self.poll_interval)

    def _read_visible_messages(self) -> list:
        """
        Read all visible messages from the current chat window.

        Steps:
        1. Get the message area region from WindowManager
        2. Screenshot the region
        3. OCR the entire area for text
        4. Deduplicate against previously seen messages
        5. Return new message objects

        Returns:
            List of message dicts ready for the message queue
        """
        region = self.window_manager.get_message_region()
        if not region or region[2] <= 0 or region[3] <= 0:
            self.logger.debug("Message region not available")
            return []

        msg_x, msg_y, msg_w, msg_h = region
        if msg_w < 50 or msg_h < 50:
            return []

        try:
            session_name = self._detect_current_session_name(msg_x, msg_y, msg_w)
            session_id, session_name = self._sync_session_context(session_name)

            # Screenshot the message area
            screenshot = self.image_processor.take_screenshot(
                region=[msg_x, msg_y, msg_w, msg_h]
            )
            if screenshot is None:
                return []
            if self._is_blank_message_area(screenshot):
                return []

            detections = self.image_processor.detect_objects(
                screenshot, imgsz=self._resolve_yolo_imgsz(screenshot)
            )
            visual_items, yolo_attempted = self._build_visual_items(
                detections=detections,
                screenshot=screenshot,
                region_offset=(msg_x, msg_y),
                session_id=session_id,
                target_name=session_name,
            )
            if not self._primed:
                self._primed = True
                self.logger.info(
                    "VisualMessageService primed with %s visible messages for '%s'",
                    len(visual_items),
                    session_name or session_id,
                )
                return []
            if visual_items:
                return visual_items
            if yolo_attempted:
                return []

            # Fallback for cases where YOLO misses a non-empty pane.
            ocr_results = self.ocr_processor.process_image(image=screenshot)
            if not ocr_results:
                return []
            return self._build_fallback_items(
                ocr_results,
                region_offset=(msg_x, msg_y),
                session_id=session_id,
                target_name=session_name,
            )

        except Exception as e:
            self.logger.error(f"Error reading visible messages: {e}")
            return []

    def _build_visual_items(
        self,
        detections: list[dict],
        screenshot: Image.Image,
        region_offset: tuple[int, int],
        session_id: str,
        target_name: str,
    ) -> tuple[list[dict], bool]:
        message_boxes = [
            item
            for item in detections
            if item.get("label") in self.yolo_message_labels
            and item.get("confidence", 0) >= 0.55
        ]
        if not message_boxes:
            return [], False

        name_boxes = [
            item
            for item in detections
            if item.get("label") == "name" and item.get("confidence", 0) >= 0.45
        ]
        message_boxes = self._dedupe_boxes(message_boxes)
        message_boxes.sort(
            key=lambda item: (item["pixel_bbox"][1], item["pixel_bbox"][0])
        )

        messages = []
        for box in message_boxes:
            crop_hash = self._hash_detection_image(screenshot, box["pixel_bbox"])
            if crop_hash:
                visual_hash = f"{session_id}|{crop_hash}"
                if visual_hash in self._seen_visual_hashes:
                    continue
            text = self._ocr_detection_box(screenshot, box["pixel_bbox"])
            if not text:
                continue
            if crop_hash:
                self._seen_visual_hashes[visual_hash] = time.time()
                while len(self._seen_visual_hashes) > self.dedup_cache_size * 4:
                    self._seen_visual_hashes.popitem(last=False)

            absolute_bbox = self._offset_bbox(box["pixel_bbox"], region_offset)
            sender_name = self._nearest_sender_name(
                screenshot=screenshot,
                message_bbox=box["pixel_bbox"],
                name_boxes=name_boxes,
                session_id=session_id,
            )
            msg_data = self._make_message_data(
                text=text,
                region=absolute_bbox,
                source="visual:yolo",
                visual_type=str(box.get("label") or "text"),
                confidence=float(box.get("confidence") or 0),
                session_id=session_id,
                target_name=target_name,
                sender=sender_name,
                is_self=self._is_self_message_box(box["pixel_bbox"], screenshot.width),
                is_chatroom=bool(sender_name),
            )
            if msg_data:
                messages.append(msg_data)
        return messages, True

    def _build_fallback_items(
        self,
        ocr_results: list[dict],
        region_offset: tuple[int, int],
        session_id: str,
        target_name: str,
    ) -> list[dict]:
        messages = []
        for result in ocr_results:
            text = result.get("label", "").strip()
            if not text:
                continue
            bbox = result.get("pixel_bbox", [0, 0, 0, 0])
            msg_data = self._make_message_data(
                text=text,
                region=self._offset_bbox(bbox, region_offset),
                source="visual:ocr",
                visual_type="text",
                confidence=float(result.get("confidence") or 0),
                session_id=session_id,
                target_name=target_name,
                is_self=False,
                is_chatroom=False,
            )
            if msg_data:
                messages.append(msg_data)
        return messages

    def _ocr_detection_box(self, screenshot: Image.Image, bbox: list[float]) -> str:
        width, height = screenshot.size
        x1, y1, x2, y2 = [int(round(value)) for value in bbox]
        pad_x = 6
        pad_y = 4
        crop_box = (
            max(0, x1 - pad_x),
            max(0, y1 - pad_y),
            min(width, x2 + pad_x),
            min(height, y2 + pad_y),
        )
        if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
            return ""
        ocr_results = self.ocr_processor.process_image(image=screenshot.crop(crop_box))
        texts = [item.get("label", "").strip() for item in ocr_results]
        return " ".join(text for text in texts if text).strip()

    def _nearest_sender_name(
        self,
        screenshot: Image.Image,
        message_bbox: list[float],
        name_boxes: list[dict],
        session_id: str,
    ) -> str:
        if not name_boxes:
            return ""

        msg_x1, msg_y1, msg_x2, _ = message_bbox
        best_box = None
        best_score = float("inf")
        for name_box in name_boxes:
            name_x1, name_y1, name_x2, name_y2 = name_box["pixel_bbox"]
            vertical_gap = msg_y1 - name_y2
            if vertical_gap < -6 or vertical_gap > 70:
                continue
            name_center_x = (name_x1 + name_x2) / 2
            if name_center_x < msg_x1 - 80 or name_center_x > msg_x2 + 20:
                continue

            x_penalty = abs(name_x1 - msg_x1) / 4
            score = vertical_gap + x_penalty
            if score < best_score:
                best_score = score
                best_box = name_box

        if not best_box:
            return ""
        return self._ocr_context_box(
            screenshot=screenshot,
            bbox=best_box["pixel_bbox"],
            session_id=session_id,
        )

    def _ocr_context_box(
        self,
        screenshot: Image.Image,
        bbox: list[float],
        session_id: str,
    ) -> str:
        crop_hash = self._hash_detection_image(screenshot, bbox)
        cache_key = f"{session_id}|{crop_hash}"
        if crop_hash and cache_key in self._context_text_cache:
            return self._context_text_cache[cache_key]

        width, height = screenshot.size
        x1, y1, x2, y2 = [int(round(value)) for value in bbox]
        crop_box = (
            max(0, x1 - 8),
            max(0, y1 - 8),
            min(width, x2 + 12),
            min(height, y2 + 8),
        )
        if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
            return ""

        ocr_results = self.ocr_processor.process_image(image=screenshot.crop(crop_box))
        text = " ".join(
            item.get("label", "").strip()
            for item in ocr_results
            if item.get("label", "").strip()
        )
        text = " ".join(text.split())
        if crop_hash:
            self._context_text_cache[cache_key] = text
            while len(self._context_text_cache) > self.dedup_cache_size * 2:
                self._context_text_cache.popitem(last=False)
        return text

    def _hash_detection_image(self, screenshot: Image.Image, bbox: list[float]) -> str:
        width, height = screenshot.size
        x1, y1, x2, y2 = [int(round(value)) for value in bbox]
        crop_box = (
            max(0, x1),
            max(0, y1),
            min(width, x2),
            min(height, y2),
        )
        if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
            return ""
        crop = screenshot.crop(crop_box).convert("L").resize((32, 16))
        return hashlib.md5(crop.tobytes()).hexdigest()

    def _make_message_data(
        self,
        text: str,
        region: list[float],
        source: str,
        visual_type: str,
        confidence: float,
        session_id: str,
        target_name: str,
        sender: str = "",
        is_self: bool = False,
        is_chatroom: bool = False,
    ) -> Optional[dict]:
        text = " ".join(text.split())
        if not text:
            return None

        text_hash = self._hash_text(
            f"{session_id}|{visual_type}|{text}|{self._bucket_region(region)}"
        )
        if text_hash in self._seen_hashes:
            return None

        self._seen_hashes[text_hash] = time.time()
        while len(self._seen_hashes) > self.dedup_cache_size:
            self._seen_hashes.popitem(last=False)

        return {
            "type": "text",
            "content": text,
            "timestamp": time.time(),
            "source": source,
            "session_id": session_id,
            "target": target_name,
            "sender": sender,
            "region": region,
            "visual_type": visual_type,
            "confidence": confidence,
            "is_self": is_self,
            "is_chatroom": is_chatroom,
        }

    def _is_self_message_box(self, bbox: list[float], image_width: int) -> bool:
        x1, _, x2, _ = bbox
        box_width = max(1, x2 - x1)
        right_margin = image_width - x2
        left_margin = x1
        return right_margin < 90 and left_margin > box_width

    def _detect_current_session_name(
        self, msg_x: int, msg_y: int, msg_w: int
    ) -> str:
        fallback = self._window_manager_session_name()
        if msg_y < 32 or msg_w < 160:
            return fallback or self._current_session_name

        title_h = max(26, min(46, msg_y - 18))
        title_y = max(0, msg_y - title_h - 4)
        title_w = max(120, min(msg_w - 140, 420))
        title_region = [msg_x + 12, title_y, title_w, title_h]

        title_image = self.image_processor.take_screenshot(region=title_region)
        if title_image is None:
            return fallback or self._current_session_name

        title_hash = self._hash_image(title_image, size=(96, 16))
        if title_hash == self._last_title_image_hash and self._last_title_text:
            return self._last_title_text

        ocr_results = self.ocr_processor.process_image(image=title_image)
        text = " ".join(
            item.get("label", "").strip()
            for item in ocr_results
            if item.get("label", "").strip()
        )
        title = self._normalize_session_name(text)
        if title:
            self._last_title_image_hash = title_hash
            self._last_title_text = title
            return title
        return fallback or self._current_session_name

    def _sync_session_context(self, session_name: str) -> tuple[str, str]:
        session_name = session_name or self._window_manager_session_name()
        if not session_name:
            session_name = self._current_session_name
        session_id = session_name or self._current_session_id or "visual_current"

        if session_id != self._current_session_id:
            if self._current_session_id:
                self.logger.info(
                    "Visual session changed: '%s' -> '%s'; priming visible messages",
                    self._current_session_name or self._current_session_id,
                    session_name or session_id,
                )
            self._current_session_id = session_id
            self._current_session_name = session_name
            self._seen_hashes.clear()
            self._seen_visual_hashes.clear()
            self._last_messages.clear()
            self._context_text_cache.clear()
            self._primed = False
        return session_id, session_name

    def _window_manager_session_name(self) -> str:
        if hasattr(self.window_manager, "get_current_session_name"):
            return str(self.window_manager.get_current_session_name() or "")
        return ""

    def _normalize_session_name(self, text: str) -> str:
        text = " ".join(text.split())
        text = re.sub(r"[\(（]\s*\d+\s*[\)）]\s*$", "", text).strip()
        return text

    def _hash_image(self, image: Image.Image, size: tuple[int, int]) -> str:
        thumb = image.convert("L").resize(size)
        return hashlib.md5(thumb.tobytes()).hexdigest()

    def _dedupe_boxes(self, boxes: list[dict]) -> list[dict]:
        selected = []
        for box in sorted(
            boxes, key=lambda item: item.get("confidence", 0), reverse=True
        ):
            if any(
                self._iou(box["pixel_bbox"], other["pixel_bbox"]) > 0.45
                for other in selected
            ):
                continue
            selected.append(box)
        return selected

    def _iou(self, a: list[float], b: list[float]) -> float:
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
        inter = iw * ih
        area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
        area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
        union = area_a + area_b - inter
        return inter / union if union else 0

    def _offset_bbox(
        self, bbox: list[float], offset: tuple[int, int]
    ) -> list[float]:
        ox, oy = offset
        return [bbox[0] + ox, bbox[1] + oy, bbox[2] + ox, bbox[3] + oy]

    def _bucket_region(self, region: list[float]) -> str:
        bucket = max(1, self.dedup_region_bucket)
        return ",".join(str(int(value // bucket)) for value in region)

    def _is_blank_message_area(self, image: Image.Image) -> bool:
        """
        Detect an empty WeChat message pane before invoking OCR.

        RapidOCR logs a warning for completely blank images. In the visual poller
        that is a normal idle state, so filter near-uniform panes up front.
        """
        try:
            width, height = image.size
            if width <= 0 or height <= 0:
                return True

            margin_x = min(16, max(4, width // 40))
            margin_y = min(16, max(4, height // 80))
            if width > margin_x * 2 and height > margin_y * 2:
                sample = image.crop(
                    (margin_x, margin_y, width - margin_x, height - margin_y)
                )
            else:
                sample = image

            sample = sample.convert("L")
            max_side = max(sample.size)
            if max_side > 512:
                scale = 512 / max_side
                sample = sample.resize(
                    (
                        max(1, int(sample.width * scale)),
                        max(1, int(sample.height * scale)),
                    )
                )

            gray_stddev = ImageStat.Stat(sample).stddev[0]
            if gray_stddev >= 1.4:
                return False

            edges = sample.filter(ImageFilter.FIND_EDGES)
            hist = edges.histogram()
            total = sum(hist) or 1
            edge_density = sum(hist[9:]) / total
            is_blank = edge_density < 0.018
            if is_blank:
                self.logger.debug(
                    "Message area appears blank: stddev=%.3f edge_density=%.5f",
                    gray_stddev,
                    edge_density,
                )
            return is_blank
        except Exception as e:
            self.logger.debug("Blank message area check failed: %s", e)
            return False

    def _hash_text(self, text: str) -> str:
        """Generate a stable hash of message text for deduplication."""
        # Normalize: strip extra whitespace, lowercase
        normalized = " ".join(text.lower().split())
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()

    def _resolve_yolo_imgsz(self, image: Image.Image) -> int | list[int]:
        resolved = self.image_processor.resolve_yolo_imgsz(
            image,
            imgsz=self.yolo_imgsz,
            stride=self.yolo_stride,
        )
        self._last_yolo_imgsz = resolved
        return resolved

    def clear_dedup_cache(self):
        """Clear the deduplication cache (e.g., after switching chats)."""
        self._seen_hashes.clear()
        self._seen_visual_hashes.clear()
        self._last_messages.clear()
        self._context_text_cache.clear()
        self._primed = False
        self.logger.debug("Dedup cache cleared")
