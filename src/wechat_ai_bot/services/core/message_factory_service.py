"""
消息工厂服务模块。
提供消息工厂相关的服务接口。
"""

import logging

from wechat_ai_bot.models import UserInfo
from wechat_ai_bot.weixin.message_classes import Message, MessageType, VisualTextMessage
from wechat_ai_bot.weixin.message_factory import FACTORY_REGISTRY

try:
    from wechat_ai_bot.services.core.database_service import DatabaseService
except ImportError:
    DatabaseService = None  # type: ignore


class MessageFactoryService:
    def __init__(self, user_info: UserInfo, db=None):
        self.logger = logging.getLogger(__name__)
        self.user_info = user_info
        self.db = db

    def create_message(self, message) -> Message:
        """将消息转换为Message对象"""
        if isinstance(message, dict):
            return self._create_visual_message(message)

        # TODO 加缓存，考虑到复杂程度，先不加了，腾讯在sqlite中索引加的不少，测试直接查询速度不慢
        table_name, msg_with_db = message
        type_ = msg_with_db[2]
        self.logger.info(f"消息类型: {MessageType.name(type_)}")
        room = None
        if self.db:
            try:
                room = self.db.get_room_by_md5(table_name.replace("Msg_", ""))
            except Exception:
                pass
        if type_ not in FACTORY_REGISTRY:
            type_ = -1
        if type_ == -1:
            self.logger.error(f"该消息类型: {type_} 未找到对应的工厂")
            return None
        contact = None
        if self.db:
            try:
                contact = self.db.get_contact_by_sender_id(msg_with_db[4], msg_with_db[17])
            except Exception:
                pass
        if not contact:
            self.logger.warn(f"未找到联系人: {msg_with_db[4]}")
            # TODO 有些消息是允许没有发送人的？这个时候怎么搞？是不是把他当作系统呢？
        msg = FACTORY_REGISTRY[type_].create(
            msg_with_db, self.user_info, self.db, contact, room
        )
        msg.room = room
        if contact:
            msg.contact = contact
        return msg

    def _create_visual_message(self, message: dict) -> Message:
        type_name = message.get("type", "text")
        if type_name != "text":
            self.logger.debug("Falling back to visual text message for type: %s", type_name)
        return VisualTextMessage(
            content=str(message.get("content") or ""),
            user_info=self.user_info,
            create_time=int(message.get("timestamp") or 0),
            target_name=str(message.get("target") or ""),
            session_id=str(message.get("session_id") or ""),
            region=message.get("region"),
            source=str(message.get("source") or "visual"),
        )
