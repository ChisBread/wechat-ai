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
import time
import threading
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
    ):
        self.logger = logging.getLogger(__name__)
        self.window_manager = window_manager
        self.image_processor = image_processor
        self.ocr_processor = ocr_processor
        self.message_queue = message_queue
        self.poll_interval = poll_interval
        self.max_message_age = max_message_age
        self.dedup_cache_size = dedup_cache_size

        self.is_running = False
        self._thread: Optional[threading.Thread] = None

        # Deduplication: LRU cache of seen message hashes
        self._seen_hashes: OrderedDict = OrderedDict()
        # Track the last message text per contact for change detection
        self._last_messages: dict = {}

    def start(self):
        """Start the polling thread."""
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="VisualMessagePoller")
        self._thread.start()
        self.logger.info(f"VisualMessageService started (poll interval: {self.poll_interval}s)")

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
                        self.logger.debug(f"No new messages detected ({consecutive_failures} polls)")
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
            # Screenshot the message area
            screenshot = self.image_processor.take_screenshot(
                region=[msg_x, msg_y, msg_w, msg_h]
            )
            if screenshot is None:
                return []
            if self._is_blank_message_area(screenshot):
                return []

            # OCR the entire message area
            ocr_results = self.ocr_processor.process_image(image=screenshot)

            if not ocr_results:
                return []

            # Extract text blocks and assemble messages
            new_messages = []
            for result in ocr_results:
                text = result.get("label", "").strip()
                if not text or len(text) < 1:
                    continue

                # Generate a hash for deduplication
                text_hash = self._hash_text(text)

                if text_hash in self._seen_hashes:
                    continue

                # New message detected
                self._seen_hashes[text_hash] = time.time()
                # Maintain dedup cache size
                while len(self._seen_hashes) > self.dedup_cache_size:
                    self._seen_hashes.popitem(last=False)

                # Build a basic message dict (will be enriched by MessageFactoryService)
                msg_data = {
                    "type": "text",  # Default; YOLO can refine this
                    "content": text,
                    "timestamp": time.time(),
                    "source": "visual",
                    "session_id": self.window_manager.get_current_session_id()
                    if hasattr(self.window_manager, "get_current_session_id")
                    else "",
                    "target": self.window_manager.get_current_session_name()
                    if hasattr(self.window_manager, "get_current_session_name")
                    else "",
                    "region": [
                        msg_x + result.get("pixel_bbox", [0, 0, 0, 0])[0],
                        msg_y + result.get("pixel_bbox", [0, 0, 0, 0])[1],
                        msg_x + result.get("pixel_bbox", [0, 0, 0, 0])[2],
                        msg_y + result.get("pixel_bbox", [0, 0, 0, 0])[3],
                    ],
                }
                new_messages.append(msg_data)

            return new_messages

        except Exception as e:
            self.logger.error(f"Error reading visible messages: {e}")
            return []

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

    def clear_dedup_cache(self):
        """Clear the deduplication cache (e.g., after switching chats)."""
        self._seen_hashes.clear()
        self._last_messages.clear()
        self.logger.debug("Dedup cache cleared")
