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
            screenshot = self.image_processor.take_screenshot(
                region=region,
                save_path="/config/runtime_images/pat_message_region.png",
            )
            if screenshot is None:
                return False
            avatar = self._find_avatar_for_pat(screenshot, action)
            if not avatar:
                self.logger.warning("No visible avatar found for pat action")
                return False
            bbox = avatar.get("pixel_bbox")
            click_x, click_y = self._avatar_safe_click_point(bbox)
            screen_x = click_x + region[0]
            screen_y = click_y + region[1]
            self.logger.info("Pat right-click avatar at %s,%s bbox=%s", screen_x, screen_y, bbox)
            human_like_mouse_move(screen_x, screen_y)
            pyautogui.click(screen_x, screen_y, button="right")
            time.sleep(self.controller.window_manager.action_delay)
            return self.find_and_click_menu_item("拍一拍")
        finally:
            self._cleanup()

    def _avatar_safe_click_point(self, bbox) -> tuple[int, int]:
        x1, y1, x2, y2 = [int(v) for v in bbox]
        width = max(1, x2 - x1)
        height = max(1, y2 - y1)
        return x1 + max(4, min(width - 4, int(width * 0.42))), y1 + height // 2

    def _find_avatar_for_pat(self, screenshot, action: PatAction):
        avatars = [
            item
            for item in self.image_processor.detect_objects(image=screenshot)
            if item.get("label") == "avatar"
        ]
        self.logger.info(
            "Pat avatar detection: total=%s target=%s user=%s is_chatroom=%s",
            len(avatars),
            action.target,
            action.user_name,
            action.is_chatroom,
        )
        try:
            self.image_processor.draw_boxes_on_screen(
                screenshot.copy(),
                avatars,
                output_path="/config/runtime_images/pat_avatars.png",
            )
        except Exception as exc:
            self.logger.debug("Failed to save pat avatar debug image: %s", exc)
        left_avatars = [
            item for item in avatars
            if item.get("pixel_bbox") and item["pixel_bbox"][0] / max(self.window_manager.MSG_WIDTH, 1) < 0.5
        ]
        self.logger.info("Pat left-side avatar candidates: %s", len(left_avatars))
        if not left_avatars:
            return None
        if not action.is_chatroom or not action.user_name:
            left_avatars.sort(key=lambda item: item.get("pixel_bbox", [0, 0, 0, 0])[1], reverse=True)
            return left_avatars[0]

        names = self.ocr_processor.find_text(image=screenshot, target_text=action.user_name)
        self.logger.info("Pat OCR name matches for %r: %s", action.user_name, len(names))
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
