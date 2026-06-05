"""
Wayland Window Manager for WeChat-AI Bot on Webtop (KDE/Wayland).
Replaces X11-based LinuxWindowManager with Wayland-native tools.
"""

import logging
import subprocess
import time
from typing import Optional, Tuple

import wechat_ai_bot.utils.mouse as pyautogui

from wechat_ai_bot.rpa.image_processor import ImageProcessor
from wechat_ai_bot.rpa.ocr_processor import OCRProcessor
from wechat_ai_bot.utils.helpers import get_center_point, set_clipboard_text
from wechat_ai_bot.utils.mouse import human_like_mouse_move
from wechat_ai_bot.utils.size_config import suggest_size


class WaylandWindowManager:
    """Wayland-native window manager for WeChat automation."""

    def __init__(
        self,
        image_processor: ImageProcessor,
        ocr_processor: OCRProcessor,
        rpa_config: dict = None,
    ):
        self.logger = logging.getLogger(__name__)
        self.size_config = suggest_size()
        self.weixin_windows = {}
        self.current_window = None
        self.ROOM_SIDE_BAR_WIDTH = 0
        self.MSG_TOP_X = 0
        self.MSG_TOP_Y = 0
        self.MSG_WIDTH = 0
        self.MSG_HEIGHT = 0
        self.SIDE_BAR_WIDTH = 0
        self.SESSION_LIST_WIDTH = 0
        self.TITLE_BAR_HEIGHT = 0

        self.ICON_CONFIGS = {
            "send_button": {"name": "send", "color": "red", "position": None},
            "search_icon": {"name": "search", "color": "yellow", "position": None},
        }

        self.image_processor = image_processor
        self.ocr_processor = ocr_processor
        self.last_switch_session = None
        self.last_switch_session_time = None
        self.rpa_config = rpa_config or {}

        self.action_delay = self.rpa_config.get("action_delay", 0.3)
        self.side_bar_delay = self.rpa_config.get("side_bar_delay", 3)
        self.scroll_delay = self.rpa_config.get("scroll_delay", 1)
        self.switch_contact_delay = self.rpa_config.get("switch_contact_delay", 0.3)

    # ---- Pixel-scanning layout init (platform-agnostic) ----

    def init_chat_window(self) -> bool:
        """Initialize chat window layout via pixel scanning."""
        self.logger.info("Initializing chat window layout (Wayland)...")
        try:
            # Verify WeChat is running
            if not self._is_wechat_running():
                self.logger.error("WeChat process not found")
                return False

            # Position WeChat window at (0,0) with expected size
            self._position_wechat_window()

            time.sleep(self.scroll_delay)
            init_result = self._init_window_part_size()
            if init_result:
                self.weixin_windows["WeChat"] = {
                    "MSG_TOP_X": self.MSG_TOP_X,
                    "MSG_TOP_Y": self.MSG_TOP_Y,
                    "MSG_WIDTH": self.MSG_WIDTH,
                    "MSG_HEIGHT": self.MSG_HEIGHT,
                    "region": [0, 0, self.size_config.width, self.size_config.height],
                }
                self.current_window = self.weixin_windows["WeChat"]
                return True
            return False
        except Exception as e:
            self.logger.error(f"Error initializing chat window: {e}")
            return False

    def _position_wechat_window(self):
        """Move WeChat window to (0,0) and resize to expected dimensions."""
        try:
            # Find the main WeChat window
            result = subprocess.run(
                ["xdotool", "search", "--class", "wechat"],
                capture_output=True, text=True, timeout=5
            )
            wids = result.stdout.strip().split()
            if not wids:
                self.logger.warning("No WeChat window found to position")
                return

            wid = wids[0]
            w = self.size_config.width
            h = self.size_config.height

            self.logger.info(f"Positioning WeChat window {wid} to 0,0 {w}x{h}")
            subprocess.run(
                ["xdotool", "windowmove", wid, "0", "0"],
                timeout=5
            )
            subprocess.run(
                ["xdotool", "windowsize", wid, str(w), str(h)],
                timeout=5
            )
            time.sleep(1)
        except Exception as e:
            self.logger.warning(f"Failed to position WeChat window: {e}")

    def _is_wechat_running(self) -> bool:
        """Check if WeChat process is running."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "wechat"], capture_output=True, text=True
            )
            return result.returncode == 0 and bool(result.stdout.strip())
        except Exception:
            return False

    def _init_window_part_size(self) -> bool:
        """Pixel-scan screenshot to identify UI regions (platform-agnostic)."""
        self.logger.info(f"Preset screen size: {self.size_config.width}x{self.size_config.height}")

        pyautogui.moveTo(150, 150)
        pyautogui.scroll(10000)
        time.sleep(self.action_delay)
        pyautogui.click()
        time.sleep(self.scroll_delay)

        # Screenshot and pixel-scan (same logic as LinuxWindowManager)
        screenshot = self.image_processor.take_screenshot(
            region=[0, 0, self.size_config.width, self.size_config.height],
        )
        pixels = screenshot.load()

        # Scan side bar and session list boundaries
        SIDE_BAR_WIDTH = 0
        SESSION_LIST_WIDTH = 0
        breakPoint = []
        j = 10
        for i in range(10, self.size_config.width * 2 // 3):
            if i > 10 and pixels[i, j] != pixels[i - 1, j]:
                breakPoint.append(i)
                if len(breakPoint) == 4:
                    break

        if len(breakPoint) < 4:
            self.logger.error("Failed to find sidebar boundaries")
            return False

        SIDE_BAR_WIDTH = breakPoint[1]
        SESSION_LIST_WIDTH = breakPoint[3] - SIDE_BAR_WIDTH
        self.MSG_TOP_X = breakPoint[3]
        breakPoint.clear()

        # Scan title bar height
        j = SIDE_BAR_WIDTH + SESSION_LIST_WIDTH + 3
        for i in range(10, 500):
            if pixels[j, i] != pixels[j, i - 1]:
                breakPoint.append(i)
                if len(breakPoint) == 4:
                    break

        if not breakPoint:
            return False
        TITLE_BAR_HEIGHT = breakPoint[0]
        self.MSG_TOP_Y = TITLE_BAR_HEIGHT

        # Scan message area dimensions
        breakPoint.clear()
        j = self.MSG_TOP_X + 2
        for i in range(10, self.size_config.height - 10):
            if i > 10 and pixels[j, i] != pixels[j, i - 1]:
                breakPoint.append(i)
                if len(breakPoint) == 4:
                    break

        if len(breakPoint) < 3:
            self.logger.warning("Message area not found")
            return False

        TITLE_BAR_HEIGHT = breakPoint[1]
        self.MSG_TOP_Y = TITLE_BAR_HEIGHT
        MSG_HEIGHT = breakPoint[3] - TITLE_BAR_HEIGHT - 2
        MSG_WIDTH = self.size_config.width - SIDE_BAR_WIDTH - SESSION_LIST_WIDTH - 2
        self.MSG_WIDTH = MSG_WIDTH
        self.MSG_HEIGHT = MSG_HEIGHT

        # Scan send button position
        send_btn_bbox = [0, 0, 0, 0]
        for i in range(20, 200):
            if (
                pixels[self.size_config.width - 1 - i, self.size_config.height - 1 - i]
                != pixels[self.size_config.width - 1 - i, self.size_config.height - 1 - i - 1]
            ):
                send_btn_bbox[2] = self.size_config.width - 1 - i
                send_btn_bbox[3] = self.size_config.height - 1 - i
                break

        button_right_x = send_btn_bbox[2] - 3
        button_right_y = send_btn_bbox[3] - 3
        for i in range(0, 200):
            if button_right_x - i > 0 and (
                pixels[button_right_x - i, button_right_y]
                != pixels[button_right_x - i - 1, button_right_y]
            ):
                send_btn_bbox[0] = button_right_x - i
                break

        self.ICON_CONFIGS["send_button"]["position"] = send_btn_bbox
        self.SIDE_BAR_WIDTH = SIDE_BAR_WIDTH
        self.SESSION_LIST_WIDTH = SESSION_LIST_WIDTH
        self.TITLE_BAR_HEIGHT = TITLE_BAR_HEIGHT

        self.logger.info(f"Layout: MSG={self.MSG_WIDTH}x{self.MSG_HEIGHT}, send_btn={send_btn_bbox}")
        return True

    def get_message_region(self) -> Optional[list]:
        if self.current_window:
            return [
                self.current_window.get("MSG_TOP_X"),
                self.current_window.get("MSG_TOP_Y"),
                self.current_window.get("MSG_WIDTH"),
                self.current_window.get("MSG_HEIGHT"),
            ]
        return None

    # ---- Session switching ----

    def switch_session(self, target: str) -> bool:
        self.logger.info(f"Switching to: {target}")
        if self.last_switch_session == target:
            return True

        time.sleep(self.action_delay)
        pyautogui.hotkey("ctrl", "f")
        time.sleep(self.action_delay)
        if not set_clipboard_text(target):
            return False
        time.sleep(self.action_delay)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(self.action_delay)
        pyautogui.press("enter")
        time.sleep(self.switch_contact_delay)
        self.last_switch_session = target
        self.last_switch_session_time = time.time()
        return True

    def activate_input_box(self, offset_x: int = 0) -> bool:
        try:
            send_button = self.get_icon_position("send_button")
            if not send_button:
                return False
            send_x, send_y = get_center_point(send_button)
            pyautogui.click(self.MSG_TOP_X + 50 + offset_x, send_y)
            time.sleep(self.action_delay)
            pyautogui.click(self.MSG_TOP_X + 50 + offset_x, send_y)
            return True
        except Exception as e:
            self.logger.error(f"Error activating input box: {e}")
            return False

    def get_icon_position(self, icon_name: str) -> Optional[list]:
        if icon_name in self.ICON_CONFIGS:
            return self.ICON_CONFIGS[icon_name].get("position")
        return None
