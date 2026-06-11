"""Shared Linux window/popup helpers for RPA handlers."""

import time
from typing import Any, Tuple

import wechat_ai_bot.utils.mouse as pyautogui
from wechat_ai_bot.rpa.linux_window_manager import WindowTypeEnum
from wechat_ai_bot.utils.helpers import get_center_point
from wechat_ai_bot.utils.mouse import human_like_mouse_move


class WindowOperationsMixin:
    def get_window_region(self, window: Any, margin: int | None = None) -> Tuple[int, int, int, int]:
        if margin is None:
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
        if menu_window:
            region = self.get_window_region(menu_window, margin=2)
            if self._find_and_click_menu_item_in_region(menu_text, region):
                return True
        return self._find_and_click_menu_item_near_mouse(menu_text)

    def _find_and_click_menu_item_in_region(self, menu_text: str, region: Tuple[int, int, int, int]) -> bool:
        try:
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

    def _find_and_click_menu_item_near_mouse(self, menu_text: str) -> bool:
        try:
            x, y = pyautogui.position()
            screen = pyautogui.size()
            left = max(0, int(x) - 80)
            top = max(0, int(y) - 80)
            region = [
                left,
                top,
                min(420, max(1, int(screen.width) - left)),
                min(620, max(1, int(screen.height) - top)),
            ]
            self.logger.info("MenuWindow not found; OCR menu fallback region=%s", region)
            return self._find_and_click_menu_item_in_region(menu_text, region)
        except Exception as exc:
            self.logger.error("菜单 OCR 兜底失败: %s", exc)
            return False
