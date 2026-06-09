import time
from dataclasses import dataclass, field

import wechat_ai_bot.utils.mouse as pyautogui
from wechat_ai_bot.rpa.action_handlers.base_handler import (
    BaseActionHandler,
    RPAAction,
    RPAActionType,
)
from wechat_ai_bot.rpa.action_handlers.mixins.group_operations_mixin import (
    GroupOperationsMixin,
)
from wechat_ai_bot.rpa.action_handlers.mixins.window_operations_mixin import (
    WindowOperationsMixin,
)
from wechat_ai_bot.rpa.linux_window_manager import WindowTypeEnum
from wechat_ai_bot.utils.helpers import set_clipboard_text


@dataclass
class RemoveRoomMemberAction(RPAAction):
    user_name: str = field(default="")
    target: str = field(default="")

    def __post_init__(self):
        self.action_type = RPAActionType.REMOVE_ROOM_MEMBER
        self.is_send_message = False


class RemoveRoomMemberHandler(WindowOperationsMixin, GroupOperationsMixin, BaseActionHandler):
    """Remove a member from a group through the group settings sidebar."""

    def execute(self, action: RemoveRoomMemberAction) -> bool:
        try:
            if not action.target or not action.user_name:
                self.logger.error("RemoveRoomMemberAction target/user_name is empty")
                return False
            if not self.window_manager.switch_session(action.target):
                return False
            if not self.window_manager.open_close_sidebar():
                return False
            if not self._click_remove_button():
                self.logger.error("未找到移出按钮")
                return False
            popup = self.window_manager.wait_for_window(WindowTypeEnum.RemoveMemberWindow, timeout=8)
            if not popup:
                self.logger.error("移出群成员窗口未打开")
                return False
            return self._select_member_and_confirm(action, popup)
        finally:
            self._cleanup()

    def _click_remove_button(self) -> bool:
        for _ in range(4):
            region = self._get_room_side_bar_region()
            buttons = self.ui_helper.find_text_elements("移出", region=region, fuzzy=80)
            if buttons:
                buttons.sort(
                    key=lambda item: (
                        item.get("pixel_bbox", [0, 0, 0, 0])[3],
                        item.get("pixel_bbox", [0, 0, 0, 0])[0],
                    ),
                    reverse=True,
                )
                self.ui_helper.click_element(
                    buttons[0].get("pixel_bbox"),
                    offset=getattr(self.window_manager, "room_action_offset", (0, -30)),
                )
                return True
            self._scroll_room_sidebar(-5)
        return False

    def _select_member_and_confirm(self, action: RemoveRoomMemberAction, popup) -> bool:
        if not set_clipboard_text(action.user_name):
            return False
        time.sleep(self.controller.window_manager.action_delay)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(self.controller.window_manager.action_delay)
        region = self.get_window_region(popup)
        matches = self.ui_helper.find_text_elements(
            text=action.user_name,
            region=region,
            fuzzy=70,
        )
        if not matches:
            self.logger.error("未找到待移出成员: %s", action.user_name)
            return False
        matches.sort(key=lambda item: item.get("pixel_bbox", [0, 0, 0, 0])[1], reverse=True)
        self.ui_helper.click_element(matches[0].get("pixel_bbox"))
        time.sleep(self.controller.window_manager.action_delay)
        return self._click_confirm_button(region)
