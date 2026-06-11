"""Shared Linux group-sidebar helpers for RPA handlers."""

import time
from typing import Optional, Tuple

import wechat_ai_bot.utils.mouse as pyautogui
from wechat_ai_bot.rpa.linux_window_manager import WindowTypeEnum
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

    def _confirm_room_input_change(self, timeout: float = 3) -> bool:
        deadline = time.time() + max(0.5, timeout)
        center_region = self._get_center_confirm_region()

        while time.time() < deadline:
            confirm = self._poll_confirm_window(
                timeout=max(0.1, min(0.5, deadline - time.time()))
            )
            if confirm and self._click_confirm_button(self.get_window_region(confirm)):
                return True

            # Recent Linux WeChat builds often render this confirmation as an
            # in-window overlay instead of a separate X11 window. Limit the
            # fallback to the center dialog area so the chat send button cannot
            # be mistaken for a confirmation.
            if self._click_confirm_button(center_region):
                return True

            remaining = deadline - time.time()
            if remaining > 0:
                time.sleep(min(0.5, remaining))

        self.logger.warning("未找到群设置输入确认按钮")
        return False

    def _trigger_room_input_confirmation(self) -> None:
        if self._poll_confirm_window(timeout=0.5):
            return
        try:
            self.window_manager.open_close_sidebar(close=True)
        except Exception as exc:
            self.logger.warning("触发群设置输入确认失败: %s", exc)

    def _poll_confirm_window(self, timeout: float = 0.5):
        get_window = getattr(self.window_manager, "get_window", None)
        if callable(get_window):
            deadline = time.time() + max(0.1, timeout)
            while time.time() < deadline:
                confirm = get_window(WindowTypeEnum.RoomInputConfirmBox)
                if confirm:
                    return confirm
                time.sleep(0.1)
            return None
        return self.window_manager.wait_for_window(
            WindowTypeEnum.RoomInputConfirmBox,
            timeout=timeout,
        )

    def _get_center_confirm_region(self) -> list[int]:
        width = int(getattr(self.window_manager.size_config, "width", 0) or 0)
        height = int(getattr(self.window_manager.size_config, "height", 0) or 0)
        if width <= 0 or height <= 0:
            return [0, 0, 1, 1]
        region_width = max(320, min(720, int(width * 0.7)))
        region_height = max(220, min(520, int(height * 0.45)))
        left = max(0, (width - region_width) // 2)
        top = max(0, (height - region_height) // 2)
        return [left, top, region_width, region_height]

    def _get_popup_action_region(self, region) -> list[int]:
        return [
            int(region[0]) + int(region[2]) // 2,
            int(region[1]) + int(region[3]) // 2,
            max(1, int(region[2]) // 2),
            max(1, int(region[3]) // 2),
        ]

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
        elements = self.ui_helper.find_text_elements(
            text=text,
            region=region,
            fuzzy=fuzzy,
            save_path="/config/runtime_images/sidebar_text_ocr.png",
        )
        if not elements:
            self.logger.debug("未找到侧栏文本: text=%s region=%s", text, region)
            return False
        elements.sort(key=lambda item: item.get("pixel_bbox", [0, 0, 0, 0])[1])
        bbox = elements[0].get("pixel_bbox")
        center = get_center_point(bbox)
        height = max(1, bbox[3] - bbox[1])
        human_like_mouse_move(center[0], int(center[1] + height * below_multiplier))
        pyautogui.click()
        time.sleep(self.controller.window_manager.action_delay)
        return True

    def _click_confirm_button(
        self,
        region: Optional[Tuple[int, int, int, int]] = None,
        texts: Optional[Tuple[str, ...]] = None,
        btn_types: Tuple[BtnType, ...] = (BtnType.GREEN,),
    ) -> bool:
        if region is None:
            region = [
                0,
                0,
                int(self.window_manager.size_config.width),
                int(self.window_manager.size_config.height),
            ]
        texts = texts or ("确定", "完成", "保存", "修改", "确认")
        button = self.ui_helper.find_and_click_text_candidate(
            texts=texts,
            region=region,
            fuzzy=76,
            save_path="/config/runtime_images/confirm_button_ocr.png",
        )
        if button:
            return True
        for btn_type in btn_types:
            button = self.ui_helper.find_btn_by_text(
                text="",
                btn_type=btn_type,
                region=region,
                min_area=250,
            )
            if button:
                center = get_center_point(button)
                human_like_mouse_move(center[0], center[1])
                pyautogui.click()
                time.sleep(self.controller.window_manager.action_delay)
                return True
        return False

    def _confirm_invite_member(self, initial_region=None) -> bool:
        clicked = False
        if initial_region is not None:
            clicked = self._click_confirm_button(
                initial_region,
                texts=("确定", "完成", "保存", "确认", "邀请"),
                btn_types=(BtnType.GREEN,),
            )
        if clicked:
            time.sleep(self.controller.window_manager.action_delay)
        confirm = self.window_manager.wait_for_window(
            WindowTypeEnum.InviteConfirmWindow,
            timeout=5,
        )
        if confirm:
            return self._click_confirm_button(
                self.get_window_region(confirm),
                texts=("确定", "完成", "保存", "确认", "邀请"),
                btn_types=(BtnType.GREEN,),
            )
        return clicked

    def _confirm_destructive_member_action(
        self,
        initial_region=None,
        confirm_window_type: WindowTypeEnum = WindowTypeEnum.RoomInputConfirmBox,
        timeout: float = 5,
    ) -> bool:
        clicked = False
        if initial_region is not None:
            clicked = self._click_confirm_button(
                initial_region,
                texts=("确定", "确认", "完成", "移出", "删除", "退出", "退出群聊"),
                btn_types=(BtnType.RED, BtnType.GREEN),
            )
        if clicked:
            time.sleep(self.controller.window_manager.action_delay)
        confirm = self.window_manager.wait_for_window(confirm_window_type, timeout=timeout)
        if confirm:
            return self._click_confirm_button(
                self.get_window_region(confirm),
                texts=("确定", "确认", "完成", "移出", "删除", "退出", "退出群聊"),
                btn_types=(BtnType.RED, BtnType.GREEN),
            )
        return clicked
