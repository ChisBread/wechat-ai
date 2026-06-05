"""
common public module package.
Contains config, exceptions, queues and other common base capabilities.
"""

from wechat_ai_bot.common.config import Config
from wechat_ai_bot.common.queues import message_queue, rpa_task_queue
from wechat_ai_bot.common.exceptions import BotException

__all__ = ["Config", "message_queue", "rpa_task_queue", "BotException"]
