import time
from dataclasses import dataclass, field

import wechat_ai_bot.utils.mouse as pyautogui
from wechat_ai_bot.rpa.action_handlers.base_handler import (
    BaseActionHandler,
    RPAAction,
    RPAActionType,
)
from wechat_ai_bot.rpa.action_handlers.mixins.window_operations_mixin import (
    WindowOperationsMixin,
)
from wechat_ai_bot.utils.helpers import get_center_point
from wechat_ai_bot.utils.mouse import human_like_mouse_move


@dataclass
class PatAction(RPAAction):
    target: str = field(default="")
    user_name: str = field(default="")
    is_chatroom: bool = field(default=False)

    def __post_init__(self):
        self.action_type = RPAActionType.PAT
        self.is_send_message = True


class PatHandler(WindowOperationsMixin, BaseActionHandler):
    """Right-click a visible avatar and choose 拍一拍."""

    def execute(self, action: PatAction) -> bool:
        try:
            if not action.target:
                self.logger.error("PatAction target is empty")
                return False
            if not self.window_manager.switch_session(action.target):
                return False
            region = self.window_manager.get_message_region()
            if not region:
                return False
            screenshot = self.image_processor.take_screenshot(region=region)
            if screenshot is None:
                return False
            avatar = self._find_avatar_for_pat(screenshot, action)
            if not avatar:
                self.logger.warning("No visible avatar found for pat action")
                return False
            bbox = avatar.get("pixel_bbox")
            center = get_center_point(bbox)
            human_like_mouse_move(center[0] + region[0], center[1] + region[1])
            pyautogui.click(button="right")
            time.sleep(self.controller.window_manager.action_delay)
            return self.find_and_click_menu_item("拍一拍")
        finally:
            self._cleanup()

    def _find_avatar_for_pat(self, screenshot, action: PatAction):
        avatars = [
            item
            for item in self.image_processor.detect_objects(image=screenshot)
            if item.get("label") == "avatar"
        ]
        left_avatars = [
            item for item in avatars
            if item.get("pixel_bbox") and item["pixel_bbox"][0] / max(self.window_manager.MSG_WIDTH, 1) < 0.5
        ]
        if not left_avatars:
            return None
        if not action.is_chatroom or not action.user_name:
            left_avatars.sort(key=lambda item: item.get("pixel_bbox", [0, 0, 0, 0])[1], reverse=True)
            return left_avatars[0]

        names = self.ocr_processor.find_text(image=screenshot, target_text=action.user_name)
        if not names:
            return None
        best = None
        best_delta = 10_000
        for name in names:
            name_y = name.get("pixel_bbox", [0, 0, 0, 0])[1]
            for avatar in left_avatars:
                avatar_y = avatar.get("pixel_bbox", [0, 0, 0, 0])[1]
                delta = abs(avatar_y - name_y)
                if delta < best_delta:
                    best = avatar
                    best_delta = delta
        return best if best_delta < 60 else None
