"""Openbox/X11 window manager using xdotool + mss."""

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

from wechat_ai_bot.rpa.image_processor import ImageProcessor
from wechat_ai_bot.rpa.ocr_processor import OCRProcessor
from wechat_ai_bot.utils.helpers import get_center_point, set_clipboard_text
from wechat_ai_bot.utils.size_config import suggest_size


def _xdotool(*args):
    """Run xdotool command."""
    subprocess.run(["xdotool"] + list(args), capture_output=True, timeout=5)


def _run(*args) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=5)


def _position() -> tuple:
    r = subprocess.run(["xdotool", "getmouselocation"], capture_output=True, text=True, timeout=3)
    import re
    x = int(re.search(r"x:(\d+)", r.stdout).group(1))
    y = int(re.search(r"y:(\d+)", r.stdout).group(1))
    return (x, y)


class XFCEWindowManager:
    """X11 window manager using xdotool + mss."""

    MIN_MAIN_WINDOW_WIDTH = 500
    MIN_MAIN_WINDOW_HEIGHT = 400

    def __init__(self, image_processor: ImageProcessor, ocr_processor: OCRProcessor, rpa_config: dict = None):
        self.logger = logging.getLogger(__name__)
        self.rpa_config = rpa_config or {}
        self.size_config = suggest_size(self.rpa_config)
        self.weixin_windows = {}
        self.current_window = None
        self.MSG_TOP_X = self.MSG_TOP_Y = self.MSG_WIDTH = self.MSG_HEIGHT = 0
        self.SIDE_BAR_WIDTH = self.SESSION_LIST_WIDTH = self.TITLE_BAR_HEIGHT = 0
        self.target_window_size = (self.size_config.width, self.size_config.height)
        self.actual_window_geometry = {}

        self.ICON_CONFIGS = {
            "send_button": {"name": "send", "color": "red", "position": None},
            "search_icon": {"name": "search", "color": "yellow", "position": None},
        }

        self.image_processor = image_processor
        self.ocr_processor = ocr_processor
        self.last_switch_session = None
        self.current_session_name = ""
        self.action_delay = self.rpa_config.get("action_delay", 0.3)
        self.scroll_delay = self.rpa_config.get("scroll_delay", 1)
        self.switch_contact_delay = self.rpa_config.get("switch_contact_delay", 0.3)
        self.window_show_delay = self.rpa_config.get("window_show_delay", 1.5)

    def _is_main_wechat_window(self, geom: dict) -> bool:
        return (
            geom["width"] >= self.MIN_MAIN_WINDOW_WIDTH
            and geom["height"] >= self.MIN_MAIN_WINDOW_HEIGHT
        )

    def _find_wechat_window(self) -> Optional[str]:
        candidates = []
        seen = set()
        for query in (
            ("search", "--class", "wechat"),
            ("search", "--name", "WeChat"),
            ("search", "--name", "微信"),
        ):
            r = _run("xdotool", *query)
            for wid in r.stdout.strip().split():
                if wid in seen:
                    continue
                seen.add(wid)
                geom = self._window_geometry(wid)
                if not geom:
                    continue
                name = _run("xdotool", "getwindowname", wid).stdout.strip()
                area = geom["width"] * geom["height"]
                candidates.append((area, wid, geom, name))

        visible = [
            item for item in candidates
            if item[3] in {"WeChat", "微信"}
            and item[2]["width"] >= 250
            and item[2]["height"] >= 300
        ]
        if not visible:
            self.logger.warning("No visible WeChat window candidates: %s", candidates)
            return None

        visible.sort(reverse=True, key=lambda item: item[0])
        area, wid, geom, name = visible[0]
        self.logger.info(
            "Selected WeChat window %s '%s' at %s,%s %sx%s",
            wid,
            name,
            geom["x"],
            geom["y"],
            geom["width"],
            geom["height"],
        )
        return wid

    def _find_green_button_center(self, geom: dict) -> Optional[tuple[int, int]]:
        screenshot = self.image_processor.take_screenshot(
            region=[geom["x"], geom["y"], geom["width"], geom["height"]],
            save_path="/config/runtime_images/wechat_entry_window.png",
        )
        if screenshot is None:
            return None

        pixels = screenshot.load()
        width, height = screenshot.size
        min_x, min_y = width, height
        max_x = max_y = -1
        count = 0

        for y in range(height // 3, height - 30):
            for x in range(20, width - 20):
                r, g, b = pixels[x, y]
                if g >= 150 and r <= 60 and b <= 120:
                    min_x = min(min_x, x)
                    min_y = min(min_y, y)
                    max_x = max(max_x, x)
                    max_y = max(max_y, y)
                    count += 1

        if count < 500 or max_x - min_x < 60 or max_y - min_y < 20:
            return None
        return geom["x"] + (min_x + max_x) // 2, geom["y"] + (min_y + max_y) // 2

    def _enter_wechat_if_needed(self, wid: str) -> bool:
        geom = self._window_geometry(wid)
        if not geom:
            return False
        if self._is_main_wechat_window(geom):
            return True

        self.logger.info(
            "Detected small WeChat window %s at %s,%s %sx%s",
            wid,
            geom["x"],
            geom["y"],
            geom["width"],
            geom["height"],
        )
        button_center = self._find_green_button_center(geom)
        if not button_center:
            self.logger.warning(
                "WeChat is not at the main chat window and no enter button was found; waiting for login/confirmation"
            )
            return False

        click_x, click_y = button_center
        self.logger.info("Clicking WeChat entry button at %s,%s", click_x, click_y)
        _run("xdotool", "windowraise", wid)
        _run("xdotool", "windowactivate", wid)
        time.sleep(self.action_delay)
        _xdotool("mousemove", "--sync", str(click_x), str(click_y))
        _xdotool("mousedown", "1")
        time.sleep(0.15)
        _xdotool("mouseup", "1")
        time.sleep(max(self.window_show_delay, 3))
        return False

    def _display_size(self) -> tuple[int, int]:
        try:
            r = _run("xdotool", "getdisplaygeometry")
            parts = r.stdout.strip().split()
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
        except Exception:
            pass
        return self.size_config.width, self.size_config.height

    def _window_geometry(self, wid: str) -> Optional[dict]:
        try:
            r = _run("xdotool", "getwindowgeometry", "--shell", wid)
            if r.returncode != 0:
                return None
            geom = {}
            for line in r.stdout.splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key in {"X", "Y", "WIDTH", "HEIGHT"}:
                    geom[key.lower()] = int(value)
            if {"x", "y", "width", "height"} <= set(geom):
                return geom
        except Exception as e:
            self.logger.warning(f"Failed to read window geometry: {e}")
        return None

    def _target_window_size(self) -> tuple[int, int]:
        display_w, display_h = self._display_size()
        target_w = min(self.size_config.width, display_w)
        target_h = min(self.size_config.height, display_h)
        if target_w != self.size_config.width or target_h != self.size_config.height:
            self.logger.warning(
                "Adjusted target window size from %sx%s to %sx%s for display %sx%s",
                self.size_config.width,
                self.size_config.height,
                target_w,
                target_h,
                display_w,
                display_h,
            )
            self.size_config.width = target_w
            self.size_config.height = target_h
        self.target_window_size = (target_w, target_h)
        return target_w, target_h

    def _find_vertical_boundary(
        self,
        screenshot,
        x_min: int,
        x_max: int,
        y_min: int,
        y_max: int,
    ) -> int:
        pixels = screenshot.load()
        width, height = screenshot.size
        x_min = max(1, min(x_min, width - 2))
        x_max = max(x_min + 1, min(x_max, width - 1))
        y_min = max(1, min(y_min, height - 2))
        y_max = max(y_min + 1, min(y_max, height - 1))
        best_x = x_min
        best_score = -1
        for x in range(x_min, x_max):
            score = 0
            samples = 0
            for y in range(y_min, y_max, 24):
                left = pixels[x - 1, y]
                right = pixels[x, y]
                score += sum(abs(int(right[i]) - int(left[i])) for i in range(3))
                samples += 1
            score = score / max(samples, 1)
            if score > best_score:
                best_score = score
                best_x = x
        return best_x

    def _find_horizontal_boundary(
        self,
        screenshot,
        y_min: int,
        y_max: int,
        x_min: int,
        x_max: int,
    ) -> int:
        pixels = screenshot.load()
        width, height = screenshot.size
        x_min = max(1, min(x_min, width - 2))
        x_max = max(x_min + 1, min(x_max, width - 1))
        y_min = max(1, min(y_min, height - 2))
        y_max = max(y_min + 1, min(y_max, height - 1))
        best_y = y_min
        best_score = -1
        for y in range(y_min, y_max):
            score = 0
            samples = 0
            for x in range(x_min, x_max, 24):
                top = pixels[x, y - 1]
                bottom = pixels[x, y]
                score += sum(abs(int(bottom[i]) - int(top[i])) for i in range(3))
                samples += 1
            score = score / max(samples, 1)
            if score > best_score:
                best_score = score
                best_y = y
        return best_y

    def _find_full_width_horizontal_boundary(
        self,
        screenshot,
        y_min: int,
        y_max: int,
        x_min: int,
        x_max: int,
        diff_threshold: int = 18,
        min_strong_ratio: float = 0.55,
        min_avg_diff: float = 20,
    ) -> Optional[int]:
        pixels = screenshot.load()
        width, height = screenshot.size
        x_min = max(1, min(x_min, width - 2))
        x_max = max(x_min + 1, min(x_max, width - 1))
        y_min = max(1, min(y_min, height - 2))
        y_max = max(y_min + 1, min(y_max, height - 1))

        best_y = None
        best_score = -1
        for y in range(y_min, y_max):
            total = 0
            strong = 0
            samples = 0
            for x in range(x_min, x_max, 4):
                top = pixels[x, y - 1]
                bottom = pixels[x, y]
                diff = sum(abs(int(bottom[i]) - int(top[i])) for i in range(3))
                total += diff
                samples += 1
                if diff > diff_threshold:
                    strong += 1
            if not samples:
                continue
            strong_ratio = strong / samples
            avg_diff = total / samples
            if strong_ratio < min_strong_ratio or avg_diff < min_avg_diff:
                continue
            score = strong_ratio * 1000 + avg_diff
            if score > best_score:
                best_score = score
                best_y = y
        return best_y

    def init_chat_window(self) -> bool:
        self.logger.info("Initializing chat window (Openbox/X11)...")
        try:
            if not subprocess.run(["pgrep", "-f", "wechat"], capture_output=True).stdout.strip():
                self.logger.error("WeChat not running"); return False

            wid = self._find_wechat_window()
            if not wid: self.logger.error("WeChat window not found"); return False
            if not self._enter_wechat_if_needed(wid):
                wid = self._find_wechat_window()
                if not wid:
                    self.logger.error("WeChat main window not found after entry click")
                    return False
                geom = self._window_geometry(wid)
                if not geom or not self._is_main_wechat_window(geom):
                    self.logger.warning("WeChat main window is not ready yet")
                    return False

            w, h = self._target_window_size()
            _run("xdotool", "windowactivate", wid)
            time.sleep(self.action_delay)
            _run("xdotool", "windowmove", wid, "0", "0")
            _run("xdotool", "windowsize", wid, str(w), str(h))
            time.sleep(self.window_show_delay)
            geom = self._window_geometry(wid)
            if geom:
                self.actual_window_geometry = geom
                self.size_config.width = geom["width"]
                self.size_config.height = geom["height"]
                w, h = geom["width"], geom["height"]
                self.logger.info("WeChat geometry: x=%s y=%s %sx%s", geom["x"], geom["y"], w, h)
            else:
                self.logger.warning("Using target geometry without window manager confirmation")

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
        self.logger.info(f"Initializing Linux WeChat layout from {w}x{h} screenshot...")

        screenshot = self.image_processor.take_screenshot(region=[0, 0, w, h])
        if screenshot is None: self.logger.error("Screenshot failed"); return False

        sidebar = self._find_vertical_boundary(screenshot, 45, 80, 40, h - 40)
        session_right = self._find_vertical_boundary(screenshot, 260, 380, 40, h - 40)
        if not 45 <= sidebar <= 80:
            sidebar = 60
        if session_right <= sidebar + 180:
            session_right = min(w - 300, sidebar + 265)

        self.SIDE_BAR_WIDTH = sidebar
        self.SESSION_LIST_WIDTH = session_right - sidebar
        self.MSG_TOP_X = session_right

        title_bottom = self._find_full_width_horizontal_boundary(
            screenshot,
            52,
            82,
            self.MSG_TOP_X + 10,
            w - 10,
            diff_threshold=8,
            min_strong_ratio=0.45,
            min_avg_diff=6,
        )
        if title_bottom is None:
            title_bottom = self._find_horizontal_boundary(
                screenshot,
                52,
                82,
                self.MSG_TOP_X + 10,
                w - 10,
            )
        if not 52 <= title_bottom <= 82:
            title_bottom = 70
        self.TITLE_BAR_HEIGHT = title_bottom
        self.MSG_TOP_Y = title_bottom

        input_top = self._find_full_width_horizontal_boundary(
            screenshot,
            max(self.MSG_TOP_Y + 200, h - 360),
            h - 80,
            self.MSG_TOP_X + 10,
            w - 10,
        )
        if input_top is None:
            input_top = self._find_horizontal_boundary(
                screenshot,
                max(self.MSG_TOP_Y + 200, h - 360),
                h - 80,
                self.MSG_TOP_X + 10,
                w - 10,
            )
        if input_top <= self.MSG_TOP_Y + 200:
            input_top = h - 150

        self.MSG_WIDTH = w - self.MSG_TOP_X
        self.MSG_HEIGHT = input_top - self.MSG_TOP_Y
        if self.MSG_WIDTH < 300 or self.MSG_HEIGHT < 300:
            self.logger.error(
                "Invalid WeChat layout: msg region %sx%s at %s,%s",
                self.MSG_WIDTH,
                self.MSG_HEIGHT,
                self.MSG_TOP_X,
                self.MSG_TOP_Y,
            )
            return False

        send = [
            max(self.MSG_TOP_X + 80, w - 116),
            max(input_top + 20, h - 50),
            w - 18,
            h - 14,
        ]
        self.ICON_CONFIGS["send_button"]["position"] = send
        self.logger.info(
            "Layout OK: sidebar=%s sessions=%s msg=(%s,%s %sx%s) input_top=%s",
            self.SIDE_BAR_WIDTH,
            self.SESSION_LIST_WIDTH,
            self.MSG_TOP_X,
            self.MSG_TOP_Y,
            self.MSG_WIDTH,
            self.MSG_HEIGHT,
            input_top,
        )
        self._save_layout_debug(screenshot)
        return True

    def _save_layout_debug(self, screenshot):
        try:
            output = "/config/runtime_images/layout_debug.png"
            boxes = [
                {
                    "pixel_bbox": [0, 0, self.SIDE_BAR_WIDTH, self.size_config.height],
                    "content": "side_bar",
                    "label": "side_bar",
                },
                {
                    "pixel_bbox": [
                        self.SIDE_BAR_WIDTH,
                        0,
                        self.SIDE_BAR_WIDTH + self.SESSION_LIST_WIDTH,
                        self.size_config.height,
                    ],
                    "content": "session_list",
                    "label": "session_list",
                },
                {
                    "pixel_bbox": [
                        self.MSG_TOP_X,
                        self.MSG_TOP_Y,
                        self.MSG_TOP_X + self.MSG_WIDTH,
                        self.MSG_TOP_Y + self.MSG_HEIGHT,
                    ],
                    "content": "message_region",
                    "label": "message_region",
                },
                {
                    "pixel_bbox": self.ICON_CONFIGS["send_button"]["position"],
                    "content": "send_button",
                    "label": "send_button",
                },
            ]
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            self.image_processor.draw_boxes_on_screen(screenshot.copy(), boxes, output_path=output)
            self.logger.info("Layout debug image saved: %s", output)
        except Exception as e:
            self.logger.warning(f"Failed to save layout debug image: {e}")

    def close_all_windows(self):
        return True

    def open_close_sidebar(self, close: bool = False):
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
        self.last_switch_session = target
        self.current_session_name = target
        return True

    def get_current_session_name(self) -> str:
        return self.current_session_name or self.last_switch_session or ""

    def get_current_session_id(self) -> str:
        return self.get_current_session_name()

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
