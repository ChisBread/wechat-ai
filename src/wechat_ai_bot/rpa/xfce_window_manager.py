"""Openbox/X11 window manager using xdotool + mss."""

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import wechat_ai_bot.utils.mouse as pyautogui
from wechat_ai_bot.rpa.linux_window_manager import WindowTypeEnum
from wechat_ai_bot.rpa.image_processor import ImageProcessor
from wechat_ai_bot.rpa.ocr_processor import OCRProcessor
from wechat_ai_bot.utils.helpers import get_center_point, set_clipboard_text
from wechat_ai_bot.utils.mouse import human_like_mouse_move
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


class XdotoolWindow:
    """Small pyautogui-like wrapper around an X11 window id."""

    def __init__(self, wid: str, title: str, geom: dict):
        self.id = str(wid)
        self.title = title
        self.left = int(geom.get("x", 0))
        self.top = int(geom.get("y", 0))
        self.width = int(geom.get("width", 0))
        self.height = int(geom.get("height", 0))

    def close(self) -> None:
        _run("xdotool", "windowclose", self.id)


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
        self.ROOM_SIDE_BAR_WIDTH = int(self.rpa_config.get("room_side_bar_width", 360))
        self.target_window_size = (self.size_config.width, self.size_config.height)
        self.actual_window_geometry = {}
        self.window_aligned = False
        self.window_id: Optional[str] = None
        self.last_window_state = "not_initialized"

        self.ICON_CONFIGS = {
            "send_button": {"name": "send", "color": "red", "position": None},
            "search_icon": {"name": "search", "color": "yellow", "position": None},
        }

        self.image_processor = image_processor
        self.ocr_processor = ocr_processor
        self.last_switch_session = None
        self.last_switch_session_time = None
        self.current_session_name = ""
        self.action_delay = self.rpa_config.get("action_delay", 0.3)
        self.side_bar_delay = self.rpa_config.get("side_bar_delay", 3)
        self.scroll_delay = self.rpa_config.get("scroll_delay", 1)
        self.switch_contact_delay = self.rpa_config.get("switch_contact_delay", 0.3)
        self.window_show_delay = self.rpa_config.get("window_show_delay", 1.5)
        self.window_margin = self.rpa_config.get("window_margin", 20)
        self.room_action_offset = tuple(self.rpa_config.get("room_action_offset", (0, -30)))
        self.search_contact_offset = tuple(self.rpa_config.get("search_contact_offset", (0, 40)))

    def _is_main_wechat_window(self, geom: dict) -> bool:
        return (
            geom["width"] >= self.MIN_MAIN_WINDOW_WIDTH
            and geom["height"] >= self.MIN_MAIN_WINDOW_HEIGHT
        )

    def _is_wechat_popup_window(self, window: XdotoolWindow) -> bool:
        if window.title.lower() not in {"wechat", "weixin", "微信"}:
            return False
        if window.width >= self.size_config.width or window.height >= self.size_config.height:
            return False
        return window.width >= 220 and window.height >= 120

    def _room_sidebar_region(self) -> list[int]:
        width = int(self.ROOM_SIDE_BAR_WIDTH or self.rpa_config.get("room_side_bar_width", 360))
        width = max(260, min(width, int(self.size_config.width) // 2))
        top = int(self.TITLE_BAR_HEIGHT or 52)
        return [
            int(self.size_config.width) - width,
            top,
            width,
            int(self.size_config.height) - top,
        ]

    def _is_room_sidebar_open(self) -> bool:
        region = self._room_sidebar_region()
        try:
            screenshot = self.image_processor.take_screenshot(
                region=region,
                save_path="/config/runtime_images/sidebar_state_ocr.png",
            )
            if screenshot is None:
                return False
            results = self.ocr_processor.process_image(image=screenshot)
        except Exception as exc:
            self.logger.debug("Failed to OCR room sidebar state: %s", exc)
            return False

        labels = [str(item.get("label") or "").strip() for item in results]
        normalized_labels = ["".join(label.split()) for label in labels if label]
        markers = (
            "搜索群成员",
            "群聊名称",
            "群公告",
            "消息免打扰",
            "我在本群的昵称",
            "查找聊天内容",
            "显示群成员昵称",
            "退出群聊",
        )
        for label in normalized_labels:
            if any(marker in label or label in marker for marker in markers):
                return True
        self.logger.debug("Room sidebar markers not found: labels=%s region=%s", labels, region)
        return False

    def _managed_window_ids(self) -> set[str]:
        """Return top-level windows known by the window manager."""
        try:
            r = _run("xprop", "-root", "_NET_CLIENT_LIST")
        except Exception:
            return set()
        if r.returncode != 0:
            return set()
        ids = set()
        for token in re.findall(r"0x[0-9a-fA-F]+", r.stdout):
            try:
                ids.add(str(int(token, 16)))
            except ValueError:
                continue
        return ids

    def _find_wechat_window(self, *, log: bool = True) -> Optional[str]:
        candidates = []
        seen = set()
        managed_ids = self._managed_window_ids()
        for query in (
            ("search", "--class", "wechat"),
            ("search", "--name", "WeChat"),
            ("search", "--name", "Weixin"),
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
                candidates.append((area, wid, geom, name, wid in managed_ids))

        managed_candidates = [item for item in candidates if item[4]]
        source = managed_candidates or candidates
        visible = [
            item for item in source
            if item[3] in {"WeChat", "Weixin", "微信"}
            and item[2]["width"] >= 250
            and item[2]["height"] >= 300
        ]
        if not visible:
            if log:
                self.logger.warning("No visible WeChat window candidates: %s", candidates)
            return None

        visible.sort(reverse=True, key=lambda item: item[0])
        area, wid, geom, name, managed = visible[0]
        if log:
            self.logger.info(
                "Selected WeChat window %s '%s' at %s,%s %sx%s managed=%s",
                wid,
                name,
                geom["x"],
                geom["y"],
                geom["width"],
                geom["height"],
                managed,
            )
        return wid

    def _all_managed_windows(self) -> list[XdotoolWindow]:
        windows: list[XdotoolWindow] = []
        for wid in self._managed_window_ids():
            geom = self._window_geometry(wid)
            if not geom:
                continue
            name = _run("xdotool", "getwindowname", wid).stdout.strip()
            if not name:
                continue
            windows.append(XdotoolWindow(wid, name, geom))
        return windows

    def _all_wechat_windows(self) -> list[XdotoolWindow]:
        windows_by_id: dict[str, XdotoolWindow] = {
            window.id: window for window in self._all_managed_windows()
        }
        for query in (
            ("search", "--class", "wechat"),
            ("search", "--name", "WeChat"),
            ("search", "--name", "Weixin"),
            ("search", "--name", "wechat"),
            ("search", "--name", "微信"),
        ):
            result = _run("xdotool", *query)
            for wid in result.stdout.strip().split():
                if not wid or wid in windows_by_id:
                    continue
                geom = self._window_geometry(wid)
                if not geom:
                    continue
                name = _run("xdotool", "getwindowname", wid).stdout.strip()
                if not name:
                    continue
                windows_by_id[wid] = XdotoolWindow(wid, name, geom)
        return list(windows_by_id.values())

    def _active_window(self) -> Optional[XdotoolWindow]:
        result = _run("xdotool", "getactivewindow")
        wid = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if not wid:
            return None
        geom = self._window_geometry(wid)
        if not geom:
            return None
        name = _run("xdotool", "getwindowname", wid).stdout.strip()
        if not name:
            return None
        return XdotoolWindow(wid, name, geom)

    def get_window(
        self,
        window_type: WindowTypeEnum,
        all_windows: bool = False,
    ) -> Optional[XdotoolWindow]:
        windows = self._all_wechat_windows() if window_type == WindowTypeEnum.MenuWindow else self._all_managed_windows()
        wechat_titles = {"wechat", "weixin", "微信"}
        if window_type == WindowTypeEnum.MainWindow:
            return next((w for w in windows if w.title.lower() in wechat_titles), None)
        if window_type == WindowTypeEnum.PublicAnnouncementWindow:
            return next((w for w in windows if "群公告" in w.title), None)
        if window_type == WindowTypeEnum.InviteMemberWindow:
            return next((w for w in windows if "添加群成员" in w.title or "邀请" in w.title), None)
        if window_type == WindowTypeEnum.RemoveMemberWindow:
            return next((w for w in windows if "移出群成员" in w.title or "删除成员" in w.title), None)
        if window_type == WindowTypeEnum.MenuWindow:
            candidates = [
                w
                for w in windows
                if w.title.lower() in wechat_titles
                and 80 <= w.width <= 600
                and 100 <= w.height <= 800
                and w.width < int(self.size_config.width) * 0.8
                and w.height < int(self.size_config.height) * 0.8
            ]
            candidates.sort(key=lambda w: w.width * w.height, reverse=True)
            return candidates[0] if candidates else None
        if window_type in (
            WindowTypeEnum.InviteConfirmWindow,
            WindowTypeEnum.InviteResonWindow,
            WindowTypeEnum.RoomInputConfirmBox,
        ):
            active = self._active_window()
            if active and self._is_wechat_popup_window(active):
                return active
            popup_windows = [
                w for w in self._all_wechat_windows() if self._is_wechat_popup_window(w)
            ]
            popup_windows.sort(key=lambda w: w.width * w.height, reverse=True)
            return popup_windows[0] if popup_windows else None
        return None

    def wait_for_window(
        self,
        window_type: WindowTypeEnum,
        all: bool = False,
        timeout: int = 5,
    ) -> Optional[XdotoolWindow]:
        start = time.time()
        while time.time() - start < timeout:
            window = self.get_window(window_type, all)
            if window:
                return window
            time.sleep(0.2)
        self.logger.warning("Window %s not found within %ss", window_type.value, timeout)
        return None

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
            self.last_window_state = "window_geometry_unavailable"
            return False
        if self._is_main_wechat_window(geom):
            self.last_window_state = "main_window"
            self.actual_window_geometry = geom
            return True

        self.last_window_state = "waiting_mobile_confirmation"
        self.actual_window_geometry = geom
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
        self.size_config = suggest_size(self.rpa_config)
        display_w, display_h = self._display_size()
        if display_w < self.size_config.width or display_h < self.size_config.height:
            self.last_window_state = "display_too_small"
            self.logger.warning(
                "Display %sx%s is smaller than target WeChat window %sx%s; waiting for Selkies display resize",
                display_w,
                display_h,
                self.size_config.width,
                self.size_config.height,
            )
            return 0, 0
        target_w = self.size_config.width
        target_h = self.size_config.height
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

    def _geometry_matches_target(
        self,
        geom: Optional[dict],
        target_w: Optional[int] = None,
        target_h: Optional[int] = None,
    ) -> bool:
        if not geom:
            return False
        target_w = target_w or self.target_window_size[0]
        target_h = target_h or self.target_window_size[1]
        tolerance = int(self.rpa_config.get("window", {}).get("size_tolerance", 8))
        return (
            abs(geom["width"] - target_w) <= tolerance
            and abs(geom["height"] - target_h) <= tolerance
            and abs(geom["x"]) <= tolerance
            and abs(geom["y"]) <= tolerance
        )

    def _clear_session_cache(self) -> None:
        self.last_switch_session = None
        self.last_switch_session_time = None
        self.current_session_name = ""

    def _set_current_layout(self, w: int, h: int) -> None:
        self.weixin_windows["WeChat"] = {
            "MSG_TOP_X": self.MSG_TOP_X,
            "MSG_TOP_Y": self.MSG_TOP_Y,
            "MSG_WIDTH": self.MSG_WIDTH,
            "MSG_HEIGHT": self.MSG_HEIGHT,
            "region": [0, 0, w, h],
        }
        self.current_window = self.weixin_windows["WeChat"]
        self.window_aligned = True

    def refresh_window_geometry(self) -> dict:
        """Refresh actual X11 geometry for dashboard/status without mutating layout."""
        wid = self._find_wechat_window(log=False)
        if not wid:
            self.last_window_state = "window_not_found"
            self.actual_window_geometry = {}
            self.window_aligned = False
            return {}

        geom = self._window_geometry(wid)
        if not geom:
            self.last_window_state = "window_geometry_unavailable"
            self.actual_window_geometry = {}
            self.window_aligned = False
            return {}

        self.window_id = wid
        self.actual_window_geometry = geom
        if not self._is_main_wechat_window(geom):
            self.last_window_state = "waiting_mobile_confirmation"
            self.window_aligned = False
        elif self._geometry_matches_target(geom):
            self.window_aligned = True
            if self.current_window:
                self.last_window_state = "ready"
        else:
            self.last_window_state = "geometry_drift"
            self.window_aligned = False
        return geom

    def _normalize_dpi(self) -> None:
        """Keep the WeChat/RPA window in physical pixels instead of Selkies HiDPI."""
        try:
            Path("/config/.Xresources").write_text("Xft.dpi: 96\n", encoding="utf-8")
            _run("xrdb", "/config/.Xresources")
        except Exception as exc:
            self.logger.debug("Failed to normalize Xft.dpi: %s", exc)

    def _resize_wechat_window(self, wid: str, target_w: int, target_h: int) -> Optional[dict]:
        """Move and resize WeChat, waiting for the WM to report the requested size."""
        tolerance = int(self.rpa_config.get("window", {}).get("size_tolerance", 8))
        for attempt in range(1, 4):
            _run("xdotool", "windowactivate", wid)
            time.sleep(self.action_delay)
            try:
                _run("wmctrl", "-ir", wid, "-b", "remove,maximized_vert,maximized_horz")
            except FileNotFoundError:
                self.logger.debug("wmctrl not available; resizing with xdotool only")
            time.sleep(self.action_delay)
            _run("xdotool", "windowmove", "--sync", wid, "0", "0")
            _run("xdotool", "windowsize", "--sync", wid, str(target_w), str(target_h))
            time.sleep(max(self.action_delay, 0.5))
            geom = self._window_geometry(wid)
            if not geom:
                continue
            width_ok = abs(geom["width"] - target_w) <= tolerance
            height_ok = abs(geom["height"] - target_h) <= tolerance
            x_ok = abs(geom["x"]) <= tolerance
            y_ok = abs(geom["y"]) <= tolerance
            if width_ok and height_ok and x_ok and y_ok:
                return geom
            self.logger.warning(
                "WeChat resize attempt %s did not reach target %sx%s: got x=%s y=%s %sx%s",
                attempt,
                target_w,
                target_h,
                geom["x"],
                geom["y"],
                geom["width"],
                geom["height"],
            )
        return self._window_geometry(wid)

    def ensure_action_ready(self, action: Any = None) -> bool:
        """Normalize WeChat state before any visual RPA action."""
        action_name = getattr(getattr(action, "action_type", None), "name", "")
        self.logger.info("Ensuring WeChat window state before RPA action: %s", action_name or "unknown")
        try:
            if not subprocess.run(["pgrep", "-x", "wechat"], capture_output=True).stdout.strip():
                self.last_window_state = "wechat_not_running"
                self.window_aligned = False
                self.logger.error("WeChat not running")
                return False

            wid = self._find_wechat_window()
            if not wid:
                self.last_window_state = "window_not_found"
                self.window_aligned = False
                return False
            if not self._enter_wechat_if_needed(wid):
                self.window_aligned = False
                return False

            self._normalize_dpi()
            w, h = self._target_window_size()
            if w <= 0 or h <= 0:
                self.window_aligned = False
                return False

            geom = self._window_geometry(wid)
            needs_relayout = not self.current_window
            if not self._geometry_matches_target(geom, w, h):
                self.logger.warning(
                    "WeChat window drifted before RPA action: got %s, target=%sx%s",
                    geom,
                    w,
                    h,
                )
                geom = self._resize_wechat_window(wid, w, h)
                self._clear_session_cache()
                needs_relayout = True
            else:
                _run("xdotool", "windowraise", wid)
                _run("xdotool", "windowactivate", wid)

            time.sleep(self.window_show_delay)
            self.actual_window_geometry = geom or {}
            self.window_id = wid
            if not self._geometry_matches_target(geom, w, h):
                self.last_window_state = "resize_mismatch"
                self.window_aligned = False
                self.logger.error("WeChat window is not aligned to target geometry")
                return False

            self.size_config.width = w
            self.size_config.height = h
            if needs_relayout:
                if not self._init_window_part_size():
                    self.last_window_state = "layout_unavailable"
                    self.window_aligned = False
                    return False
                self._set_current_layout(w, h)

            self.last_window_state = "ready"
            self.window_aligned = True
            return True
        except Exception as exc:
            self.last_window_state = "ensure_failed"
            self.window_aligned = False
            self.logger.error("Failed to ensure WeChat window state: %s", exc, exc_info=True)
            return False

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
            if not subprocess.run(["pgrep", "-x", "wechat"], capture_output=True).stdout.strip():
                self.last_window_state = "wechat_not_running"
                self.logger.error("WeChat not running"); return False

            wid = self._find_wechat_window()
            if not wid: self.logger.error("WeChat window not found"); return False
            if not self._enter_wechat_if_needed(wid):
                wid = self._find_wechat_window()
                if not wid:
                    self.last_window_state = "window_not_found"
                    self.logger.error("WeChat main window not found after entry click")
                    return False
                geom = self._window_geometry(wid)
                if not geom or not self._is_main_wechat_window(geom):
                    self.last_window_state = "waiting_mobile_confirmation"
                    if geom:
                        self.actual_window_geometry = geom
                    self.logger.warning("WeChat main window is not ready yet")
                    return False

            self._normalize_dpi()
            w, h = self._target_window_size()
            if w <= 0 or h <= 0:
                return False
            geom = self._resize_wechat_window(wid, w, h)
            time.sleep(self.window_show_delay)
            if geom:
                self.actual_window_geometry = geom
                self.window_id = wid
                self.logger.info(
                    "WeChat geometry: x=%s y=%s %sx%s target=%sx%s",
                    geom["x"],
                    geom["y"],
                    geom["width"],
                    geom["height"],
                    w,
                    h,
                )
                tolerance = int(self.rpa_config.get("window", {}).get("size_tolerance", 8))
                if (
                    abs(geom["width"] - w) > tolerance
                    or abs(geom["height"] - h) > tolerance
                    or abs(geom["x"]) > tolerance
                    or abs(geom["y"]) > tolerance
                ):
                    self.last_window_state = "resize_mismatch"
                    self.window_aligned = False
                    self.logger.warning("WeChat window is not aligned to target geometry")
                    return False
                self.size_config.width = w
                self.size_config.height = h
                self.last_window_state = "aligned"
                self.window_aligned = True
            else:
                self.logger.warning("Using target geometry without window manager confirmation")

            time.sleep(self.scroll_delay)
            if self._init_window_part_size():
                self._set_current_layout(w, h)
                self.last_window_state = "ready"
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
        configured_room_sidebar_width = int(self.rpa_config.get("room_side_bar_width", 360))
        self.ROOM_SIDE_BAR_WIDTH = max(260, min(configured_room_sidebar_width, w // 2))
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
        for window in self._all_wechat_windows():
            if window.id == self.window_id:
                continue
            is_wechat_popup = self._is_wechat_popup_window(window)
            is_named_dialog = any(
                text in window.title
                for text in ("群公告", "添加群成员", "邀请", "移出群成员", "删除成员")
            )
            if not is_wechat_popup and not is_named_dialog:
                continue
            try:
                window.close()
                time.sleep(0.1)
            except Exception:
                continue
        return True

    def open_close_sidebar(self, close: bool = False):
        try:
            w = int(self.size_config.width)
            opened = self._is_room_sidebar_open()
            if close and not opened:
                return True
            if not close and opened:
                return True
            x = max(0, w - 30)
            y = max(36, int(self.TITLE_BAR_HEIGHT or 52) - 10)
            human_like_mouse_move(x, y)
            pyautogui.click()
            time.sleep(self.side_bar_delay)
            return True
        except Exception as exc:
            self.logger.warning("Failed to toggle room sidebar: %s", exc)
            return False

    def get_message_region(self) -> Optional[list]:
        if self.current_window:
            return [self.current_window.get(k) for k in ["MSG_TOP_X","MSG_TOP_Y","MSG_WIDTH","MSG_HEIGHT"]]
        return None

    def switch_session(self, target: str) -> bool:
        self.logger.info("Switching session to: %s", target)
        time.sleep(self.action_delay); _xdotool("key", "ctrl+f")
        time.sleep(self.action_delay)
        if not set_clipboard_text(target): return False
        time.sleep(self.action_delay); _xdotool("key", "ctrl+v")
        time.sleep(self.action_delay); _xdotool("key", "Return")
        time.sleep(self.switch_contact_delay)
        self.last_switch_session = target
        self.last_switch_session_time = time.time()
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
