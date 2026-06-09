"""Shared Linux group-sidebar helpers for RPA handlers."""

import time
from typing import Tuple

import wechat_ai_bot.utils.mouse as pyautogui
from wechat_ai_bot.rpa.ui_helper import BtnType
from wechat_ai_bot.utils.helpers import get_center_point, set_clipboard_text
from wechat_ai_bot.utils.mouse import human_like_mouse_move


class GroupOperationsMixin:
    def _get_room_side_bar_region(self) -> Tuple[int, int, int, int]:
        width = int(
            getattr(self.window_manager, "ROOM_SIDE_BAR_WIDTH", 0)
            or self.window_manager.rpa_config.get("room_side_bar_width", 360)
        )
        width = max(260, min(width, int(self.window_manager.size_config.width) // 2))
        top = int(getattr(self.window_manager, "TITLE_BAR_HEIGHT", 0) or 52)
        return [
            int(self.window_manager.size_config.width) - width,
            top,
            width,
            int(self.window_manager.size_config.height) - top,
        ]

    def _replace_input_text(self, text: str) -> bool:
        if not set_clipboard_text(text):
            return False
        time.sleep(self.controller.window_manager.action_delay)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(self.controller.window_manager.action_delay)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(self.controller.window_manager.action_delay)
        pyautogui.press("enter")
        time.sleep(self.controller.window_manager.action_delay)
        return True

    def _scroll_room_sidebar(self, clicks: int) -> None:
        region = self._get_room_side_bar_region()
        human_like_mouse_move(
            region[0] + region[2] // 2,
            region[1] + region[3] // 2,
        )
        time.sleep(self.controller.window_manager.action_delay)
        pyautogui.scroll(clicks)
        time.sleep(self.controller.window_manager.scroll_delay)

    def _click_sidebar_text(
        self,
        text: str,
        *,
        fuzzy: int = 85,
        below_multiplier: float = 1.6,
    ) -> bool:
        region = self._get_room_side_bar_region()
        elements = self.ui_helper.find_text_elements(text=text, region=region, fuzzy=fuzzy)
        if not elements:
            return False
        elements.sort(key=lambda item: item.get("pixel_bbox", [0, 0, 0, 0])[1])
        bbox = elements[0].get("pixel_bbox")
        center = get_center_point(bbox)
        height = max(1, bbox[3] - bbox[1])
        human_like_mouse_move(center[0], int(center[1] + height * below_multiplier))
        pyautogui.click()
        time.sleep(self.controller.window_manager.action_delay)
        return True

    def _click_confirm_button(self, region=None) -> bool:
        if region is None:
            region = [
                0,
                0,
                int(self.window_manager.size_config.width),
                int(self.window_manager.size_config.height),
            ]
        for text in ("确定", "完成", "保存"):
            button = self.ui_helper.find_and_click_text_element(text=text, region=region)
            if button:
                return True
        button = self.ui_helper.find_btn_by_text(
            text="",
            btn_type=BtnType.GREEN,
            region=region,
        )
        if button:
            center = get_center_point(button)
            human_like_mouse_move(center[0], center[1])
            pyautogui.click()
            time.sleep(self.controller.window_manager.action_delay)
            return True
        return False
