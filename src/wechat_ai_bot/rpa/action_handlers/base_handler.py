from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from wechat_ai_bot.rpa.rpa_action import RPAActionType, RPAAction

if TYPE_CHECKING:
    from wechat_ai_bot.rpa.controller import RPAController
    from wechat_ai_bot.rpa.image_processor import ImageProcessor
    from wechat_ai_bot.rpa.input_handler import InputHandler
    from wechat_ai_bot.rpa.ocr_processor import OCRProcessor
    from wechat_ai_bot.rpa.ui_helper import UIInteractionHelper
    from wechat_ai_bot.rpa.window_manager import WindowManager


class BaseActionHandler(ABC):
    """
    RPA操作处理器基类，定义所有ActionHandler的接口和通用逻辑。
    """

    def __init__(self, controller: "RPAController"):
        """
        初始化 BaseActionHandler。

        Args:
            controller (Any): RPAController实例。
        """
        self.controller: "RPAController" = controller
        self.window_manager: "WindowManager" = controller.window_manager
        self.image_processor: "ImageProcessor" = controller.image_processor
        self.ocr_processor: "OCRProcessor" = controller.ocr_processor
        self.input_handler: "InputHandler" = controller.input_handler
        self.ui_helper: "UIInteractionHelper" = controller.ui_helper
        self.logger = controller.logger.getChild(self.__class__.__name__)

    @abstractmethod
    def execute(self, action: Any) -> bool:
        """
        执行具体的RPA操作。

        Args:
            action (Any): RPAAction实例。

        Returns:
            bool: 操作是否成功。
        """
        pass

    def _cleanup(self, close_sidebar: bool = False):
        """
        公共清理逻辑：关闭弹窗；按需关闭群设置侧边栏。
        """
        try:
            self.window_manager.close_all_windows()
            if close_sidebar:
                self.window_manager.open_close_sidebar(close=True)
        except Exception as e:
            self.logger.warning(f"清理时发生异常: {e}")
