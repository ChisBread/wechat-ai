"""Shared Linux window/popup helpers for RPA handlers."""

import time
from typing import Any, Tuple

import wechat_ai_bot.utils.mouse as pyautogui
from wechat_ai_bot.rpa.linux_window_manager import WindowTypeEnum
from wechat_ai_bot.utils.helpers import get_center_point
from wechat_ai_bot.utils.mouse import human_like_mouse_move


class WindowOperationsMixin:
    def get_window_region(self, window: Any) -> Tuple[int, int, int, int]:
        margin = int(getattr(self.controller.window_manager, "window_margin", 20))
        return [
            int(getattr(window, "left", 0)) + margin,
            int(getattr(window, "top", 0)) + margin,
            max(1, int(getattr(window, "width", 0)) - margin * 2),
            max(1, int(getattr(window, "height", 0)) - margin * 2),
        ]

    def find_and_click_menu_item(self, menu_text: str) -> bool:
        menu_window = self.controller.window_manager.wait_for_window(
            WindowTypeEnum.MenuWindow
        )
        if not menu_window:
            return False
        try:
            region = self.get_window_region(menu_window)
            results = self.ocr_processor.process_image(
                image=self.controller.image_processor.take_screenshot(
                    region=region,
                    save_path="/config/runtime_images/menu.png",
                )
            )
            for result in results:
                if result.get("label") != menu_text:
                    continue
                center = get_center_point(result.get("pixel_bbox"))
                human_like_mouse_move(
                    target_x=center[0] + region[0],
                    target_y=center[1] + region[1],
                )
                time.sleep(self.controller.window_manager.action_delay)
                pyautogui.click()
                return True
        except Exception as exc:
            self.logger.error("查找并点击菜单项失败: %s", exc)
        return False
