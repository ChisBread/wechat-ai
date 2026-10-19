from dataclasses import dataclass
from typing import Optional

from wechat_ai_bot.rpa.action_handlers.base_handler import (
    BaseActionHandler,
    RPAAction,
    RPAActionType,
)


@dataclass
class DownloadImageAction(RPAAction):
    target: Optional[str] = None
    max_count: int = 1

    def __post_init__(self):
        self.action_type = RPAActionType.DOWNLOAD_IMAGE
        self.is_send_message = False


class DownloadImageHandler(BaseActionHandler):
    """
    下载图片操作的处理器。
    """

    def execute(self, action: DownloadImageAction) -> bool:
        """
        执行下载图片操作。
        Args:
            action (DownloadImageAction): 操作对象。
        Returns:
            bool: 操作是否成功。
        """
        # There is no message-specific download/verification implementation yet.
        # Switching chats cannot establish that an original image was downloaded.
        self.logger.error("Original image download is unavailable; open the image in WeChat and download the original")
        return False
