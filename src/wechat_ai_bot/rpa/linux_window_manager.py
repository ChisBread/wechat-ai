"""
Linux X11 Window Manager for WeChat-AI Bot.
Replaces win32gui-based WindowManager with python-xlib X11 implementation.

The pixel-scanning layout initialization logic is platform-agnostic
and adapted from the original omni-bot-sdk WindowManager.
"""

import logging
import time
from enum import Enum
from typing import Any, Dict, Optional, Tuple

import wechat_ai_bot.utils.mouse as pyautogui
try:
    from Xlib import X, display
    from Xlib.protocol import event
except ImportError:
    X, display, event = None, None, None

from wechat_ai_bot.rpa.image_processor import ImageProcessor
from wechat_ai_bot.rpa.ocr_processor import OCRProcessor
from wechat_ai_bot.utils.helpers import get_center_point, set_clipboard_text
from wechat_ai_bot.utils.mouse import human_like_mouse_move
from wechat_ai_bot.utils.size_config import suggest_size



class MenuTypeEnum(Enum):
    """Menu type identifiers."""
    Chat = "聊天"
    Contact = "联系人"
    Favorite = "收藏"
    Friend = "朋友圈"
    FriendNotification = "朋友圈通知"
    FriendSend = "朋友圈发送"
    FrendRefresh = "朋友圈刷新"


class WindowTypeEnum(Enum):
    """Window type classification for WeChat popups/dialogs."""
    MainWindow = "MainWindow"
    InviteMemberWindow = "InviteMemberWindow"
    InviteConfirmWindow = "InviteConfirmWindow"
    InviteResonWindow = "InviteResonWindow"
    RemoveMemberWindow = "RemoveMemberWindow"
    AddFriendWindow = "AddFriendWindow"
    UnableInviteWindow = "UnableInviteWindow"
    SearchHistoryWindow = "SearchHistoryWindow"
    FriendWindow = "FriendWindow"
    PublicAnnouncementWindow = "PublicAnnouncementWindow"
    RoomInputConfirmBox = "RoomInputConfirmBox"
    MenuWindow = "MenuWindow"
    SearchContactWindow = "SearchContactWindow"


class X11Window:
    """Lightweight wrapper around an X11 window for pyautogui-like interface."""

    def __init__(self, wid: int, title: str, d: display.Display):
        self._wid = wid
        self.title = title
        self._d = d
        self._win = d.create_resource_object('window', wid)

    @property
    def left(self) -> int:
        geom = self._win.get_geometry()
        return geom.x

    @left.setter
    def left(self, value: int):
        self._win.configure(x=value)
        self._d.flush()

    @property
    def top(self) -> int:
        geom = self._win.get_geometry()
        return geom.y

    @top.setter
    def top(self, value: int):
        self._win.configure(y=value)
        self._d.flush()

    @property
    def width(self) -> int:
        return self._win.get_geometry().width

    @width.setter
    def width(self, value: int):
        self._win.configure(width=value)
        self._d.flush()

    @property
    def height(self) -> int:
        return self._win.get_geometry().height

    @height.setter
    def height(self, value: int):
        self._win.configure(height=value)
        self._d.flush()

    @property
    def size(self):
        return (self.width, self.height)

    @size.setter
    def size(self, value: Tuple[int, int]):
        self._win.configure(width=value[0], height=value[1])
        self._d.flush()

    @property
    def topleft(self):
        return (self.left, self.top)

    @topleft.setter
    def topleft(self, value: Tuple[int, int]):
        self._win.configure(x=value[0], y=value[1])
        self._d.flush()

    @property
    def visible(self) -> bool:
        """Check if window is mapped (visible)."""
        attrs = self._win.get_attributes()
        return attrs.map_state == X.IsViewable

    def activate(self):
        """Activate/focus this window via _NET_ACTIVE_WINDOW."""
        atom = self._d.intern_atom("_NET_ACTIVE_WINDOW")
        msg = event.ClientMessage(
            window=self._win,
            client_type=atom,
            data=(32, [1, X.CurrentTime, 0, 0, 0])
        )
        root = self._d.screen().root
        root.send_event(msg, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        self._d.flush()
        # Also raise the window
        self._win.configure(stack_mode=X.Above)
        self._d.flush()

    def restore(self):
        """Restore window from minimized state."""
        self._win.map()
        self._d.flush()

    def minimize(self):
        """Minimize (unmap) the window."""
        self._win.unmap()
        self._d.flush()

    def close(self):
        """Close window via _NET_CLOSE_WINDOW."""
        atom = self._d.intern_atom("_NET_CLOSE_WINDOW")
        msg = event.ClientMessage(
            window=self._win,
            client_type=atom,
            data=(32, [X.CurrentTime, 0, 0, 0, 0])
        )
        root = self._d.screen().root
        root.send_event(msg, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        self._d.flush()

    def get_wm_class(self) -> Optional[Tuple[str, str]]:
        """Get WM_CLASS property (instance_name, class_name)."""
        prop = self._win.get_full_property(
            self._d.intern_atom("WM_CLASS"), X.AnyPropertyType
        )
        if prop and prop.value:
            parts = prop.value.decode('utf-8', errors='replace').split('\x00')
            if len(parts) >= 2:
                return (parts[0], parts[1])
        return None


class LinuxWindowManager:
    """X11-based Window Manager for WeChat automation on Linux."""

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
            "send_button": {
                "name": "发送按钮",
                "color": "red",
                "position": None,
            },
            "search_icon": {
                "name": "搜索输入框",
                "color": "yellow",
                "position": None,
            },
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
        self.window_show_delay = self.rpa_config.get("window_show_delay", 1.5)
        self.window_margin = self.rpa_config.get("window_margin", 20)
        self.room_action_offset = tuple(
            self.rpa_config.get("room_action_offset", (0, -30))
        )
        self.search_contact_offset = tuple(
            self.rpa_config.get("search_contact_offset", (0, 40))
        )
        self.color_ranges = self.rpa_config.get("color_ranges", {})

        # X11 connection (lazy init to avoid .Xauthority issues)
        self._d = None
        self._root = None

    # ---- X11 Window Enumeration ----

    def _ensure_x11(self):
        """Lazy-init X11 connection. Called before any X11 operation."""
        if self._d is None:
            import os
            display_name = os.environ.get('DISPLAY', ':99')
            self._d = display.Display(display_name)
            self._root = self._d.screen().root
            self.logger.info(f"X11 connected to {display_name}")

    def _get_all_windows(self) -> list:
        """Enumerate all visible X11 windows with _NET_CLIENT_LIST."""
        self._ensure_x11()
        windows = []
        try:
            client_list = self._root.get_full_property(
                self._d.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType
            )
            if not client_list:
                return windows

            for wid in client_list.value:
                try:
                    win = self._d.create_resource_object('window', wid)
                    attrs = win.get_attributes()
                    if attrs.map_state != X.IsViewable:
                        continue

                    name_prop = win.get_full_property(
                        self._d.intern_atom("_NET_WM_NAME"), X.AnyPropertyType
                    ) or win.get_full_property(
                        self._d.intern_atom("WM_NAME"), X.AnyPropertyType
                    )
                    title = name_prop.value.decode('utf-8', errors='replace') if name_prop and name_prop.value else ""

                    if not title:
                        continue

                    xwin = X11Window(wid, title, self._d)
                    windows.append(xwin)
                except Exception:
                    continue
        except Exception as e:
            self.logger.error(f"Error enumerating windows: {e}")
        return windows

    def _find_window_by_title(self, title: str, exact: bool = False) -> Optional[X11Window]:
        """Find a window by title substring or exact match."""
        for win in self._get_all_windows():
            if exact and win.title == title:
                return win
            elif not exact and title in win.title:
                return win
        return None

    def _find_windows_by_wm_class(self, class_name: str) -> list:
        """Find all windows whose WM_CLASS contains the given string."""
        result = []
        for win in self._get_all_windows():
            wm_class = win.get_wm_class()
            if wm_class:
                instance, cls = wm_class
                if class_name.lower() in instance.lower() or class_name.lower() in cls.lower():
                    result.append(win)
        return result

    def _is_qt_window(self, win: X11Window) -> bool:
        """Check if a window is a Qt-based window (like WeChat)."""
        wm_class = win.get_wm_class()
        if wm_class and wm_class[1]:
            # Linux Qt apps often have class names starting with the app name
            # WeChat on Linux likely registers as "wechat" or "WeChat"
            return True  # Accept all windows for now; filter by title
        return True

    # ---- Window Activation & Management ----

    def _is_wechat_foreground(self, reposition: bool = True) -> bool:
        """Check if WeChat main window is in foreground and properly sized."""
        chat_windows = [w for w in self._get_all_windows() if "微信" in w.title]
        if not chat_windows:
            self.logger.error("WeChat window not found")
            return False

        chat_window = chat_windows[0]
        self._activate_window("微信")

        if reposition:
            chat_window.topleft = (0, 0)
            time.sleep(self.action_delay)

        chat_window.size = (self.size_config.width, self.size_config.height)
        time.sleep(self.action_delay)

        if (
            chat_window.width < self.size_config.width
            or chat_window.height < self.size_config.height
        ):
            self.logger.warning("WeChat window size mismatch, retrying...")
            return False
        else:
            self.logger.info("WeChat window size OK")
            self.size_config.width = chat_window.width
            self.size_config.height = chat_window.height
            return True

    def _activate_window(self, title: str = "微信"):
        """Activate a window by title."""
        try:
            win = self._find_window_by_title(title)
            if win:
                win.activate()
                win.restore()
                return True
        except Exception as e:
            self.logger.error(f"Error activating window '{title}': {e}")
        return False

    # ---- UI Layout Initialization (Pixel Scanning) ----

    def init_chat_window(self) -> bool:
        """Initialize chat window layout via pixel scanning."""
        self.logger.info("Initializing chat window layout...")
        try:
            if self._is_wechat_foreground():
                time.sleep(self.scroll_delay)
                init_result = self._init_window_part_size()
                if init_result:
                    self.weixin_windows["微信"] = {
                        "window": self._find_window_by_title("微信"),
                        "MSG_TOP_X": self.MSG_TOP_X,
                        "MSG_TOP_Y": self.MSG_TOP_Y,
                        "MSG_WIDTH": self.MSG_WIDTH,
                        "MSG_HEIGHT": self.MSG_HEIGHT,
                        "region": [0, 0, self.size_config.width, self.size_config.height],
                    }
                    self.current_window = self.weixin_windows["微信"]
                    return True
                return False
            else:
                self.logger.info("WeChat window not active, retrying...")
                return False
        except Exception as e:
            self.logger.error(f"Error initializing chat window: {e}")
            return False

    def _init_window_part_size(self) -> bool:
        """Pixel-scan screenshot to identify UI regions.
        This logic is platform-agnostic — uses only screenshots and pixel colors.
        """
        self.logger.info(f"Preset WeChat window size: {self.size_config.width}, {self.size_config.height}")
        pyautogui.moveTo(150, 150)
        pyautogui.scroll(10000)
        time.sleep(self.action_delay)
        pyautogui.click()
        time.sleep(self.scroll_delay)

        screenshot = self.image_processor.take_screenshot(
            region=[0, 0, self.size_config.width, self.size_config.height],
        )
        pixels = screenshot.load()

        # Scan 1: Find side bar and session list boundaries
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
            self.logger.error("Failed to find sidebar/session-list boundaries")
            return False

        SIDE_BAR_WIDTH = breakPoint[1]
        SESSION_LIST_WIDTH = breakPoint[3] - SIDE_BAR_WIDTH
        self.MSG_TOP_X = breakPoint[3]
        breakPoint.clear()

        # Scan 2: Find title bar height
        j = SIDE_BAR_WIDTH + SESSION_LIST_WIDTH + 3
        for i in range(10, 500):
            if pixels[j, i] != pixels[j, i - 1]:
                breakPoint.append(i)
                if len(breakPoint) == 4:
                    break

        TITLE_BAR_HEIGHT = breakPoint[0]
        self.MSG_TOP_Y = TITLE_BAR_HEIGHT

        # Scan 3: Find message area dimensions
        breakPoint.clear()
        j = self.MSG_TOP_X + 2
        for i in range(10, self.size_config.height - 10):
            if i > 10 and pixels[j, i] != pixels[j, i - 1]:
                breakPoint.append(i)
                if len(breakPoint) == 4:
                    break

        if len(breakPoint) < 3:
            self.logger.warning("Message area not found (right side not loaded)")
            return False

        TITLE_BAR_HEIGHT = breakPoint[1]
        self.MSG_TOP_Y = TITLE_BAR_HEIGHT
        MSG_HEIGHT = breakPoint[3] - TITLE_BAR_HEIGHT - 2
        MSG_WIDTH = self.size_config.width - SIDE_BAR_WIDTH - SESSION_LIST_WIDTH - 2
        self.MSG_WIDTH = MSG_WIDTH
        self.MSG_HEIGHT = MSG_HEIGHT
        self.logger.info(f"MSG_WIDTH: {self.MSG_WIDTH}, MSG_HEIGHT: {self.MSG_HEIGHT}")

        # Scan 4: Search box position
        search_box_point = [SIDE_BAR_WIDTH + SESSION_LIST_WIDTH // 2, 0]
        breakPoint.clear()
        for i in range(10, TITLE_BAR_HEIGHT - 2):
            if pixels[search_box_point[0], i] != pixels[search_box_point[0], i - 1]:
                breakPoint.append(i)
                if len(breakPoint) == 3:
                    break

        if len(breakPoint) >= 2:
            search_box_point[1] = (breakPoint[0] + breakPoint[-1]) // 2
        else:
            self.logger.warning("Search box position not found")
            return False

        # Scan 5: Send button position
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
        for i in range(0, 200):
            if button_right_y - i > 0 and (
                pixels[button_right_x, button_right_y - i]
                != pixels[button_right_x, button_right_y - i - 1]
            ):
                send_btn_bbox[1] = button_right_y - i
                break

        self.logger.info(f"send_btn_bbox: {send_btn_bbox}")

        # Scan 6: Sidebar menu icons
        breakPoint.clear()
        icons = []
        for i in range(TITLE_BAR_HEIGHT, self.size_config.height // 2):
            if pixels[SIDE_BAR_WIDTH // 2, i] != pixels[SIDE_BAR_WIDTH // 2, i - 1]:
                breakPoint.append(i)
                if len(icons) == 0:
                    icons.append(i)
                if len(breakPoint) > 1 and breakPoint[-1] - breakPoint[-2] > 30:
                    icons.append(breakPoint[-2])
                    icons.append(breakPoint[-1])
        icons.append(breakPoint[-1])

        menu_labels = [
            MenuTypeEnum.Chat.value,
            MenuTypeEnum.Contact.value,
            MenuTypeEnum.Favorite.value,
            MenuTypeEnum.Friend.value,
        ]

        for idx, i in enumerate(range(0, len(icons), 2)):
            bbox = [0, icons[i], SIDE_BAR_WIDTH, icons[i + 1]]
            name = menu_labels[idx] if idx < len(menu_labels) else f"Menu{idx}"
            self.ICON_CONFIGS[name] = {
                "name": name,
                "color": None,
                "position": bbox,
            }

        # Store icon positions
        self.ICON_CONFIGS["search_icon"]["position"] = [
            search_box_point[0] - 10, search_box_point[1] - 10,
            search_box_point[0] + 10, search_box_point[1] + 10,
        ]
        self.ICON_CONFIGS["send_button"]["position"] = send_btn_bbox

        self.SIDE_BAR_WIDTH = SIDE_BAR_WIDTH
        self.SESSION_LIST_WIDTH = SESSION_LIST_WIDTH
        self.TITLE_BAR_HEIGHT = TITLE_BAR_HEIGHT

        # Detect room sidebar width
        self.open_close_sidebar()
        screenshot = self.image_processor.take_screenshot(
            region=[0, 0, self.size_config.width, self.size_config.height],
        )
        pixels = screenshot.load()
        startx = SIDE_BAR_WIDTH + SESSION_LIST_WIDTH + 50
        starty = get_center_point(send_btn_bbox)[1]
        for i in range(startx, self.size_config.width):
            if pixels[i, starty] != pixels[i - 1, starty]:
                self.ROOM_SIDE_BAR_WIDTH = self.size_config.width - i
                self.logger.info(f"Room sidebar width: {self.ROOM_SIDE_BAR_WIDTH}")
                break

        self.open_close_sidebar(close=True)
        return True

    def get_window_region(self) -> Optional[Tuple[int, int, int, int]]:
        if self.current_window:
            return self.current_window.get("region")
        return None

    def get_message_region(self) -> Optional[list]:
        if self.current_window:
            return [
                self.current_window.get("MSG_TOP_X"),
                self.current_window.get("MSG_TOP_Y"),
                self.current_window.get("MSG_WIDTH"),
                self.current_window.get("MSG_HEIGHT"),
            ]
        return None

    # ---- Session & Menu Switching ----

    def switch_session(self, target: str) -> bool:
        """Switch to a chat session by name."""
        now = time.time()
        if self.last_switch_session_time and now - self.last_switch_session_time > 180:
            self.last_switch_session = None
            self.last_switch_session_time = None

        self.logger.info(f"Switching session to: {target}")
        if target in self.weixin_windows:
            self.switch_window(target)
            return True

        self.switch_window("微信")
        if self.last_switch_session == target:
            self.logger.info(f"Already on session: {target}")
            return True

        # Use ctrl+f search to switch
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

    def switch_window(self, target: str) -> bool:
        """Activate a window by name."""
        self.logger.info(f"Activating window: {target}")
        if target in self.weixin_windows:
            self.current_window = self.weixin_windows[target]
        elif "微信" in self.weixin_windows:
            self.current_window = self.weixin_windows["微信"]
        else:
            return False

        try:
            if self.current_window and self.current_window.get("window"):
                self.current_window["window"].activate()
                return True
        except Exception as e:
            self.logger.error(f"Error switching window: {e}")
        return False

    def switch_menu(self, target: str) -> bool:
        """Click a sidebar menu item."""
        self.logger.info(f"Switching menu to: {target}")
        menu = self.ICON_CONFIGS.get(target)
        if not menu:
            self.logger.error(f"Menu '{target}' not found")
            return False

        self.last_switch_session = None
        self.last_switch_session_time = None
        center = get_center_point(menu.get("position"))
        human_like_mouse_move(target_x=center[0], target_y=center[1])
        pyautogui.click()
        return True

    def long_press_menu(self, target: str, duration: int = 1) -> bool:
        """Long-press a sidebar menu item."""
        menu = self.ICON_CONFIGS.get(target)
        if not menu:
            self.logger.error(f"Menu '{target}' not found")
            return False
        self.last_switch_session = None
        self.last_switch_session_time = None
        center = get_center_point(menu.get("position"))
        human_like_mouse_move(target_x=center[0], target_y=center[1])
        pyautogui.mouseDown(button="left")
        time.sleep(duration)
        pyautogui.mouseUp(button="left")
        return True

    # ---- Input Box ----

    def activate_input_box(self, offset_x: int = 0) -> bool:
        """Click into the message input box."""
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

    # ---- Sidebar Toggle ----

    def open_close_sidebar(self, close: bool = False) -> bool:
        """Toggle the room sidebar open or closed."""
        color = self.image_processor.get_pixel_color(
            self.size_config.width - 20, self.size_config.height - 20
        )
        CLOSED = color != (255, 255, 255)

        if close and CLOSED:
            return True
        elif not close and not CLOSED:
            return True
        else:
            x = self.SIDE_BAR_WIDTH + self.SESSION_LIST_WIDTH + 50
            y_pos = self.ICON_CONFIGS.get("search_icon", {}).get("position")
            if y_pos:
                y = y_pos[1]
            else:
                y = self.TITLE_BAR_HEIGHT // 2
            human_like_mouse_move(target_x=x, target_y=y)
            pyautogui.click()
            time.sleep(self.side_bar_delay)
            return True

    # ---- Popup Window Detection ----

    def get_window(self, window_type: WindowTypeEnum, all_windows: bool = False) -> Optional[X11Window]:
        """Find a specific WeChat popup window by type."""
        all_x11_windows = self._get_all_windows()
        wechat_titles = {"wechat", "weixin", "微信"}

        if window_type == WindowTypeEnum.MainWindow:
            for win in all_x11_windows:
                if win.title.lower() in wechat_titles:
                    return win

        elif window_type == WindowTypeEnum.AddFriendWindow:
            for win in all_x11_windows:
                if "通过朋友验证" in win.title or win.title == "通过朋友验证":
                    return win
            for win in all_x11_windows:
                if win.title == "微信" and win.width < 600 and win.height < 500:
                    return win

        elif window_type == WindowTypeEnum.InviteMemberWindow:
            for win in all_x11_windows:
                if "添加群成员" in win.title:
                    return win

        elif window_type == WindowTypeEnum.RemoveMemberWindow:
            for win in all_x11_windows:
                if "移出群成员" in win.title:
                    return win

        elif window_type == WindowTypeEnum.SearchHistoryWindow:
            for win in all_x11_windows:
                if "搜索聊天记录" in win.title:
                    return win

        elif window_type == WindowTypeEnum.FriendWindow:
            for win in all_x11_windows:
                if win.title == "朋友圈":
                    return win

        elif window_type == WindowTypeEnum.PublicAnnouncementWindow:
            for win in all_x11_windows:
                if "的群公告" in win.title:
                    return win

        elif window_type in (WindowTypeEnum.InviteConfirmWindow, WindowTypeEnum.InviteResonWindow, WindowTypeEnum.RoomInputConfirmBox):
            for win in all_x11_windows:
                if win.title.lower() in wechat_titles and win.width < self.size_config.width:
                    return win

        elif window_type == WindowTypeEnum.SearchContactWindow:
            for win in all_x11_windows:
                if win.title.lower() in wechat_titles and win.left < self.SIDE_BAR_WIDTH:
                    return win

        return None

    def close_all_windows(self):
        """Close all non-main WeChat popup windows."""
        for win in self._get_all_windows():
            if win.title != "微信" and win.title != "":
                try:
                    win.close()
                except Exception:
                    pass
            time.sleep(0.1)

    def open_friend_window(self) -> Optional[X11Window]:
        """Open the friend circle (朋友圈) window."""
        self.switch_menu(MenuTypeEnum.Friend.value)
        time.sleep(self.scroll_delay)
        friend_win = self.get_window(WindowTypeEnum.FriendWindow)
        if not friend_win:
            self.switch_menu(MenuTypeEnum.Friend.value)
        return friend_win

    def wait_for_window(self, window_type: WindowTypeEnum, all: bool = False, timeout: int = 5) -> Optional[X11Window]:
        """Poll for a window until it appears or timeout."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            win = self.get_window(window_type, all)
            if win:
                return win
            time.sleep(0.2)
        self.logger.warning(f"Window {window_type.value} not found within {timeout}s")
        return None
