"""
XFCE X11 Window Manager — pure xdotool + mss, no pyautogui.
"""
import logging, subprocess, time
from typing import Optional

from wechat_ai_bot.rpa.image_processor import ImageProcessor
from wechat_ai_bot.rpa.ocr_processor import OCRProcessor
from wechat_ai_bot.utils.helpers import get_center_point, set_clipboard_text
from wechat_ai_bot.utils.size_config import suggest_size


def _xdotool(*args):
    """Run xdotool command."""
    subprocess.run(["xdotool"] + list(args), capture_output=True, timeout=5)


def _position() -> tuple:
    r = subprocess.run(["xdotool", "getmouselocation"], capture_output=True, text=True, timeout=3)
    import re
    x = int(re.search(r"x:(\d+)", r.stdout).group(1))
    y = int(re.search(r"y:(\d+)", r.stdout).group(1))
    return (x, y)


class XFCEWindowManager:
    """X11 window manager using xdotool + mss."""

    def __init__(self, image_processor: ImageProcessor, ocr_processor: OCRProcessor, rpa_config: dict = None):
        self.logger = logging.getLogger(__name__)
        self.size_config = suggest_size()
        self.weixin_windows = {}
        self.current_window = None
        self.MSG_TOP_X = self.MSG_TOP_Y = self.MSG_WIDTH = self.MSG_HEIGHT = 0
        self.SIDE_BAR_WIDTH = self.SESSION_LIST_WIDTH = self.TITLE_BAR_HEIGHT = 0

        self.ICON_CONFIGS = {
            "send_button": {"name": "send", "color": "red", "position": None},
            "search_icon": {"name": "search", "color": "yellow", "position": None},
        }

        self.image_processor = image_processor
        self.ocr_processor = ocr_processor
        self.last_switch_session = None
        self.rpa_config = rpa_config or {}
        self.action_delay = rpa_config.get("action_delay", 0.3)
        self.scroll_delay = rpa_config.get("scroll_delay", 1)
        self.switch_contact_delay = rpa_config.get("switch_contact_delay", 0.3)

    def _find_wechat_window(self) -> Optional[str]:
        r = subprocess.run(["xdotool", "search", "--class", "wechat"], capture_output=True, text=True, timeout=5)
        ids = r.stdout.strip().split()
        return ids[0] if ids else None

    def init_chat_window(self) -> bool:
        self.logger.info("Initializing chat window (XFCE/X11)...")
        try:
            if not subprocess.run(["pgrep", "-f", "wechat"], capture_output=True).stdout.strip():
                self.logger.error("WeChat not running"); return False

            wid = self._find_wechat_window()
            if not wid: self.logger.error("WeChat window not found"); return False

            w, h = self.size_config.width, self.size_config.height
            subprocess.run(["xdotool", "windowmove", wid, "0", "0"], timeout=3)
            subprocess.run(["xdotool", "windowsize", wid, str(w), str(h)], timeout=3)
            time.sleep(1)
            self.logger.info(f"WeChat positioned at 0,0 {w}x{h}")

            time.sleep(self.scroll_delay)
            if self._init_window_part_size():
                self.weixin_windows["WeChat"] = {
                    "MSG_TOP_X": self.MSG_TOP_X, "MSG_TOP_Y": self.MSG_TOP_Y,
                    "MSG_WIDTH": self.MSG_WIDTH, "MSG_HEIGHT": self.MSG_HEIGHT,
                    "region": [0, 0, w, h],
                }
                self.current_window = self.weixin_windows["WeChat"]
                return True
            return False
        except Exception as e:
            self.logger.error(f"init error: {e}"); return False

    def _init_window_part_size(self) -> bool:
        w, h = self.size_config.width, self.size_config.height
        self.logger.info(f"Pixel scanning {w}x{h}...")

        # Reset scroll position
        _xdotool("mousemove", "150", "150")
        for _ in range(100): _xdotool("click", "4")  # scroll up
        time.sleep(self.action_delay)
        _xdotool("click", "1")
        time.sleep(self.scroll_delay)

        screenshot = self.image_processor.take_screenshot(region=[0, 0, w, h])
        if screenshot is None: self.logger.error("Screenshot failed"); return False
        pixels = screenshot.load()

        # Scan - same algorithm as original
        bp, j = [], 10
        for i in range(10, w * 2 // 3):
            if i > 10 and pixels[i, j] != pixels[i-1, j]:
                bp.append(i)
                if len(bp) == 4: break
        if len(bp) < 4: self.logger.error("Sidebar boundaries not found"); return False
        self.SIDE_BAR_WIDTH, self.SESSION_LIST_WIDTH = bp[1], bp[3] - bp[1]
        self.MSG_TOP_X = bp[3]

        bp, j2 = [], self.MSG_TOP_X + 3
        for i in range(10, 500):
            if pixels[j2, i] != pixels[j2, i-1]:
                bp.append(i)
                if len(bp) == 4: break
        if not bp: return False
        self.TITLE_BAR_HEIGHT, self.MSG_TOP_Y = bp[0], bp[0]

        bp, j3 = [], self.MSG_TOP_X + 2
        for i in range(10, h - 10):
            if i > 10 and pixels[j3, i] != pixels[j3, i-1]:
                bp.append(i)
                if len(bp) == 4: break
        if len(bp) < 3: return False
        self.MSG_HEIGHT = bp[3] - bp[1] - 2
        self.MSG_WIDTH = w - self.SIDE_BAR_WIDTH - self.SESSION_LIST_WIDTH - 2

        send = [0, 0, 0, 0]
        for i in range(20, 200):
            if pixels[w-1-i, h-1-i] != pixels[w-1-i, h-1-i-1]:
                send[2], send[3] = w-1-i, h-1-i; break
        bx, by = send[2]-3, send[3]-3
        for i in range(200):
            if bx-i > 0 and pixels[bx-i, by] != pixels[bx-i-1, by]:
                send[0] = bx-i; break
        self.ICON_CONFIGS["send_button"]["position"] = send
        self.logger.info(f"Layout OK: MSG={self.MSG_WIDTH}x{self.MSG_HEIGHT}")
        return True

    def get_message_region(self) -> Optional[list]:
        if self.current_window:
            return [self.current_window.get(k) for k in ["MSG_TOP_X","MSG_TOP_Y","MSG_WIDTH","MSG_HEIGHT"]]
        return None

    def switch_session(self, target: str) -> bool:
        if self.last_switch_session == target: return True
        time.sleep(self.action_delay); _xdotool("key", "ctrl+f")
        time.sleep(self.action_delay)
        if not set_clipboard_text(target): return False
        time.sleep(self.action_delay); _xdotool("key", "ctrl+v")
        time.sleep(self.action_delay); _xdotool("key", "Return")
        time.sleep(self.switch_contact_delay)
        self.last_switch_session = target; return True

    def activate_input_box(self, offset_x: int = 0) -> bool:
        try:
            sb = self.get_icon_position("send_button")
            if not sb: return False
            sx, sy = get_center_point(sb)
            _xdotool("mousemove", str(self.MSG_TOP_X + 50 + offset_x), str(sy))
            _xdotool("click", "1")
            time.sleep(self.action_delay)
            _xdotool("click", "1")
            return True
        except Exception: return False

    def get_icon_position(self, icon_name: str) -> Optional[list]:
        return self.ICON_CONFIGS.get(icon_name, {}).get("position")
