"""
RPA Controller module — Linux port.
Contains token bucket rate limiter and RPA dispatcher.
Adapted from omni-bot-sdk for Linux (X11) platform.
"""

import logging
import random
import time
from typing import Any, Dict, Optional
from threading import Lock

from wechat_ai_bot.rpa.rpa_action import RPAAction, RPAActionType
from .image_processor import ImageProcessor
from .linux_input_handler import LinuxInputHandler  # stub, Wayland uses pyautogui directly
from .xfce_window_manager import XFCEWindowManager as LinuxWindowManager
from .message_sender import MessageSender
from .ocr_processor import OCRProcessor
from .ui_helper import UIInteractionHelper


class TokenBucket:
    """Token bucket rate limiter for controlling RPA operation rate."""

    def __init__(self, rate: float, capacity: float):
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._last_update = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_update
        if elapsed > 0:
            new_tokens = elapsed * self._rate
            self._tokens = min(self._capacity, self._tokens + new_tokens)
            self._last_update = now

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def consume(self, amount: int = 1) -> bool:
        if self._tokens >= amount:
            self._tokens -= amount
            return True
        return False


class RPAController:
    """RPA dispatcher — dependency injection hub for all RPA components."""

    def __init__(
        self,
        window_manager: LinuxWindowManager,
        ocr_processor: OCRProcessor,
        image_processor: ImageProcessor,
        rpa_config: dict,
        db=None,  # Optional: for future DB integration
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.db = db
        self.window_manager = window_manager
        self.ocr_processor = ocr_processor
        self.image_processor = image_processor
        self.message_sender = MessageSender(self.window_manager)
        self.input_handler = LinuxInputHandler(
            action_delay=rpa_config.get("action_delay", 0.3)
        )
        self.ui_helper = UIInteractionHelper(self)
        self.action_handlers: Dict[RPAActionType, Any] = {}
        self._register_handlers()

        self.short_term_limiter = TokenBucket(
            rate=rpa_config.get("short_term_rate", 0.2),
            capacity=rpa_config.get("short_term_capacity", 2),
        )
        self.long_term_limiter = TokenBucket(
            rate=rpa_config.get("long_term_rate", 0.25),
            capacity=rpa_config.get("long_term_capacity", 15),
        )
        self.rate_limiter_lock = Lock()
        self.logger.info("RPAController initialized (Linux).")

    def _register_handlers(self):
        """Register available RPA action handlers."""
        from .action_handlers import (
            DownloadFileHandler,
            DownloadImageHandler,
            DownloadVideoHandler,
            ForwardMessageHandler,
            LeaveRoomHandler,
            PublicRoomAnnouncementHandler,
            SendTextMessageHandler,
        )

        handler_classes = [
            (RPAActionType.SEND_TEXT_MESSAGE, SendTextMessageHandler),
            (RPAActionType.PUBLIC_ROOM_ANNOUNCEMENT, PublicRoomAnnouncementHandler),
            (RPAActionType.DOWNLOAD_IMAGE, DownloadImageHandler),
            (RPAActionType.DOWNLOAD_FILE, DownloadFileHandler),
            (RPAActionType.DOWNLOAD_VIDEO, DownloadVideoHandler),
            (RPAActionType.FORWARD_MESSAGE, ForwardMessageHandler),
            (RPAActionType.LEAVE_ROOM, LeaveRoomHandler),
        ]

        # Additional handlers will be added as they are ported
        for action_type, handler_class in handler_classes:
            try:
                self.action_handlers[action_type] = handler_class(self)
            except Exception as e:
                self.logger.warning(f"Failed to register handler {action_type.name}: {e}")

        self.logger.info(f"Registered {len(self.action_handlers)} action handlers")

    def _can_send_message(self) -> bool:
        with self.rate_limiter_lock:
            if (
                self.short_term_limiter.tokens >= 1
                and self.long_term_limiter.tokens >= 1
            ):
                self.short_term_limiter.consume(1)
                self.long_term_limiter.consume(1)
                return True
            return False

    def execute_action(self, action: RPAAction) -> bool:
        """Execute an RPA action, dispatching to the appropriate handler."""
        handler = self.action_handlers.get(action.action_type)
        if handler is None:
            self.logger.error(f"No handler registered for: {action.action_type}")
            return False

        if action.is_send_message:
            max_wait_seconds = 10
            wait_interval = 1
            start_time = time.monotonic()
            while not self._can_send_message():
                if time.monotonic() - start_time > max_wait_seconds:
                    self.logger.warning(f"Rate limit timeout for {action.action_type.name}")
                    return False
                self.logger.info(f"Rate limited, waiting {wait_interval}s...")
                time.sleep(wait_interval)
            self.logger.info("Rate limit passed.")
            time.sleep(random.uniform(0.5, 1.5))

        try:
            self.logger.info(f"Executing RPA action: {action.action_type.name}")
            success = handler.execute(action)
            self.logger.info(f"RPA action {action.action_type.name}: {'OK' if success else 'FAILED'}")
            return success
        except Exception as e:
            self.logger.error(f"Unhandled error in {action.action_type.name}: {e}", exc_info=True)
            return False
