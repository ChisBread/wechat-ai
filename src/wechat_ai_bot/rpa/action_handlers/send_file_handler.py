import time
from dataclasses import dataclass, field
from pathlib import Path

import wechat_ai_bot.utils.mouse as pyautogui
from wechat_ai_bot.rpa.action_handlers.base_handler import (
    BaseActionHandler,
    RPAAction,
    RPAActionType,
)
from wechat_ai_bot.utils.helpers import copy_file_to_clipboard


@dataclass
class SendFileAction(RPAAction):
    file_path: str = field(default="")
    target: str = field(default="")
    is_chatroom: bool = field(default=False)

    def __post_init__(self):
        self.action_type = RPAActionType.SEND_FILE
        self.is_send_message = True


class SendFileHandler(BaseActionHandler):
    """Send a local file by pasting a file URI into the current WeChat chat."""

    def execute(self, action: SendFileAction) -> bool:
        try:
            path = Path(action.file_path).expanduser()
            if not path.exists() or not path.is_file():
                self.logger.error("File does not exist: %s", action.file_path)
                return False
            if not action.target:
                self.logger.error("SendFileAction target is empty")
                return False
            if not self.window_manager.switch_session(action.target):
                return False
            if not copy_file_to_clipboard(str(path)):
                self.logger.error("Failed to copy file to clipboard: %s", path)
                return False
            if not self.window_manager.activate_input_box():
                return False
            time.sleep(self.controller.window_manager.action_delay)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(max(1.0, self.controller.window_manager.action_delay))
            return self.ui_helper.click_send_button()
        finally:
            self._cleanup()
