import time
from dataclasses import dataclass, field
from typing import Optional

from wechat_ai_bot.rpa.action_handlers.base_handler import (
    BaseActionHandler,
    RPAAction,
    RPAActionType,
)


@dataclass
class SendTextMessageAction(RPAAction):
    """Send a text message to a contact or room by visible name."""

    content: str = field(default="")
    target: str = field(default="")
    is_chatroom: bool = field(default=False)
    at_user_name: Optional[str] = field(default=None)
    quote_message: Optional[str] = field(default=None)
    random_at_quote: bool = field(default=False)

    def __post_init__(self):
        self.action_type = RPAActionType.SEND_TEXT_MESSAGE
        self.is_send_message = True


class SendTextMessageHandler(BaseActionHandler):
    """Handler for text-message sending via visual RPA."""

    def execute(self, action: SendTextMessageAction) -> bool:
        try:
            if not action.target:
                self.logger.error("SendTextMessageAction target is empty")
                return False
            if not self.window_manager.switch_session(action.target):
                return False
            if action.at_user_name:
                self.controller.message_sender.clear_input_box()
                if not self.controller.message_sender.mention_user(action.at_user_name):
                    self.logger.error(
                        "Failed to mention user before sending text: %s",
                        action.at_user_name,
                    )
                    return False
                time.sleep(self.controller.window_manager.action_delay)
            return self.controller.message_sender.send_message(
                action.content,
                clear_input_box=action.at_user_name is None,
            )
        finally:
            self._cleanup()
