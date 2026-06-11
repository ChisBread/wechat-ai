from dataclasses import dataclass, field

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


@dataclass
class RenameNameInRoomAction(RPAAction):
    target: str = field(default="")
    name: str = field(default="")

    def __post_init__(self):
        self.action_type = RPAActionType.RENAME_NAME_IN_ROOM
        self.is_send_message = False


class RenameNameInRoomHandler(WindowOperationsMixin, GroupOperationsMixin, BaseActionHandler):
    """Rename the current user's display name in a group chat."""

    def execute(self, action: RenameNameInRoomAction) -> bool:
        try:
            if not action.target or not action.name:
                self.logger.error("RenameNameInRoomAction target/name is empty")
                return False
            if not self.window_manager.switch_session(action.target):
                return False
            if not self.window_manager.open_close_sidebar():
                return False
            for _ in range(5):
                if self._click_sidebar_text("我在本群的昵称", fuzzy=80):
                    break
                self._scroll_room_sidebar(-5)
            else:
                self.logger.error("未找到我在本群的昵称入口")
                return False
            if not self._replace_input_text(action.name):
                return False
            self._trigger_room_input_confirmation()
            return self._confirm_room_input_change(timeout=25)
        finally:
            self._cleanup()
