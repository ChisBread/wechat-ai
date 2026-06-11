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
class Invite2RoomAction(RPAAction):
    user_name: str = field(default="")
    target: str = field(default="")

    def __post_init__(self):
        self.action_type = RPAActionType.INVITE_2_ROOM
        self.is_send_message = True


class Invite2RoomHandler(WindowOperationsMixin, GroupOperationsMixin, BaseActionHandler):
    """Invite a contact into a group through the group settings sidebar."""

    def execute(self, action: Invite2RoomAction) -> bool:
        try:
            if not action.target or not action.user_name:
                self.logger.error("Invite2RoomAction target/user_name is empty")
                return False
            if not self.window_manager.switch_session(action.target):
                return False
            if not self.window_manager.open_close_sidebar():
                return False
            if not self._click_invite_button():
                self.logger.error("未找到添加/邀请按钮")
                return False
            popup = self.window_manager.wait_for_window(WindowTypeEnum.InviteMemberWindow, timeout=8)
            if not popup:
                self.logger.error("添加群成员窗口未打开")
                return False
            return self._select_contact_and_confirm(action, popup)
        finally:
            self._cleanup()

    def _click_invite_button(self) -> bool:
        region = self._get_room_side_bar_region()
        for text in ("添加", "邀请", "+"):
            buttons = self.ui_helper.find_text_elements(text=text, region=region, fuzzy=80)
            if buttons:
                buttons.sort(
                    key=lambda item: (
                        item.get("pixel_bbox", [0, 0, 0, 0])[1],
                        item.get("pixel_bbox", [0, 0, 0, 0])[0],
                    )
                )
                self.ui_helper.click_element(
                    buttons[0].get("pixel_bbox"),
                    offset=getattr(self.window_manager, "room_action_offset", (0, -30)),
                )
                return True
        # OCR often misses icon-only plus buttons. Click the likely plus position
        # in the member grid near the top of the group settings sidebar.
        x = region[0] + min(region[2] - 48, 96)
        y = region[1] + 92
        pyautogui.click(x, y)
        time.sleep(self.controller.window_manager.action_delay)
        return True

    def _select_contact_and_confirm(self, action: Invite2RoomAction, popup) -> bool:
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
            self.logger.error("未找到待邀请联系人: %s", action.user_name)
            return False
        matches.sort(key=lambda item: item.get("pixel_bbox", [0, 0, 0, 0])[1])
        self.ui_helper.click_element(matches[0].get("pixel_bbox"))
        time.sleep(self.controller.window_manager.action_delay)
        action_region = self._get_popup_action_region(region)
        return self._confirm_invite_member(action_region)
