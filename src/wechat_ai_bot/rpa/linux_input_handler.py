"""
Input Handler — Wayland version.
Uses wechat_ai_bot utils/mouse (ydotool/wtype) for all input operations.
No X11/pyautogui/Xlib dependencies.
"""

import logging
import random
import time

from wechat_ai_bot.utils.mouse import (
    human_like_mouse_move, click, moveTo, scroll, press, hotkey, typewrite,
    mouseDown, mouseUp, position,
)


class LinuxInputHandler:
    """Input handler for Wayland via ydotool/wtype."""

    def __init__(self, action_delay: float = 0.3):
        self.logger = logging.getLogger(__name__)
        self.action_delay = action_delay

    def move_mouse(self, x: int, y: int, duration: float = 0.1):
        human_like_mouse_move(target_x=x, target_y=y, min_duration=duration)

    def do_click(self, x: int = None, y: int = None, button: str = "left", clicks: int = 1):
        click(x, y, button, clicks)
        time.sleep(self.action_delay)

    def right_click(self, x: int = None, y: int = None):
        click(x, y, "right")

    def mouse_down(self, x: int = None, y: int = None, button: str = "left"):
        if x is not None and y is not None:
            moveTo(x, y)
        mouseDown(button)

    def mouse_up(self, button: str = "left"):
        mouseUp(button)

    def do_scroll(self, clicks: int, x: int = None, y: int = None):
        scroll(clicks, x, y)
        time.sleep(self.action_delay)

    def press_key(self, key: str):
        press(key)
        time.sleep(self.action_delay)

    def do_hotkey(self, *keys):
        hotkey(*keys)
        time.sleep(self.action_delay)

    def type_text(self, text: str, interval: float = 0.02):
        typewrite(text, interval)

    def switch_to_english_input(self):
        return True  # Wayland: no-op

    def get_active_window_title(self) -> str:
        return ""

    def get_screen_size(self) -> tuple:
        return (1024, 768)
