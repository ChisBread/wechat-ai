"""Command dispatchers used by the MCP server."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict

from wechat_ai_bot.models import UserInfo
from wechat_ai_bot.rpa.rpa_action import RPAActionType
from wechat_ai_bot.weixin.message_classes import MessageType

if TYPE_CHECKING:
    from wechat_ai_bot.clients.mqtt_client import MQTTClient


class QueueCommandDispatcher:
    """
    Dispatch MCP commands directly into the in-process RPA task queue.

    This is the default path for the Docker runtime because the MCP server and bot
    live in the same Python process. MQTT remains available for external/legacy
    forwarding, but should not be required for local AI clients.
    """

    def __init__(self, bot: Any, user_info: UserInfo):
        self.bot = bot
        self.user = user_info

    def dispatch(self, topic: str, payload: Dict[str, Any]) -> None:
        """Translate a message payload into a local RPA action."""
        action = self._message_action(payload)
        self._queue_action(action)

    def dispatch_rpa(self, action_type: str, action_data: Dict[str, Any]) -> str:
        """Translate an RPA payload into a local RPA action."""
        action = self._rpa_action(action_type, action_data)
        self._queue_action(action)
        return f"RPA操作 '{str(action_type)}' 已提交到本地队列。"

    def _message_action(self, payload: Dict[str, Any]) -> Any:
        local_type = payload.get("local_type")
        if local_type in (MessageType.Text, MessageType.Text2):
            from wechat_ai_bot.rpa.action_handlers import SendTextMessageAction

            at_list = payload.get("at_list") or []
            return SendTextMessageAction(
                content=str(payload.get("message_content") or ""),
                target=str(payload.get("nickname") or payload.get("target") or ""),
                is_chatroom=bool(payload.get("is_chatroom")),
                at_user_name=at_list[0] if at_list else None,
            )
        if local_type == MessageType.File:
            raise NotImplementedError("SendFileAction has not been ported to Linux RPA yet.")
        if local_type == MessageType.Pat:
            raise NotImplementedError("PatAction has not been ported to Linux RPA yet.")
        raise NotImplementedError(f"Unsupported MCP message local_type: {local_type!r}")

    def _rpa_action(self, action_type: str, action_data: Dict[str, Any]) -> Any:
        if isinstance(action_type, RPAActionType):
            action_type_value = action_type.value
        else:
            action_type_value = str(action_type)

        if action_type_value == RPAActionType.PUBLIC_ROOM_ANNOUNCEMENT.value:
            from wechat_ai_bot.rpa.action_handlers import PublicRoomAnnouncementAction

            return PublicRoomAnnouncementAction(
                content=str(action_data.get("content") or ""),
                target=str(action_data.get("target") or ""),
                force_edit=bool(action_data.get("force_edit", False)),
            )
        if action_type_value == RPAActionType.LEAVE_ROOM.value:
            from wechat_ai_bot.rpa.action_handlers import LeaveRoomAction

            return LeaveRoomAction(target=str(action_data.get("target") or ""))

        raise NotImplementedError(
            f"RPA action '{action_type_value}' has not been ported to Linux RPA yet."
        )

    def _queue_action(self, action: Any) -> None:
        queue = getattr(self.bot, "rpa_task_queue", None)
        if queue is None:
            raise RuntimeError("RPA task queue is unavailable.")
        queue.put(action)


class UnavailableCommandDispatcher:
    """Dispatcher used when neither local bot nor MQTT forwarding is available."""

    def __init__(self, reason: str):
        self.reason = reason

    def dispatch(self, topic: str, payload: Dict[str, Any]) -> None:
        raise ConnectionError(self.reason)

    def dispatch_rpa(self, action_type: str, action_data: Dict[str, Any]) -> str:
        raise ConnectionError(self.reason)


class MqttCommandDispatcher:
    """
    使用MQTT实现的命令分发器。
    支持通用消息分发和RPA操作分发。
    """

    def __init__(self, mqtt_client: "MQTTClient", user_info: UserInfo):
        """
        初始化分发器，注入MQTT客户端和用户信息。
        """
        self.mqtt = mqtt_client
        self.user = user_info

    def dispatch(self, topic: str, payload: Dict[str, Any]) -> None:
        """
        发送通用MQTT消息。
        检查MQTT连接状态，异常时抛出错误。
        """
        if not self.mqtt.client.connected_flag or self.mqtt.client.bad_connection_flag:
            raise ConnectionError("MQTT连接不可用，请检查MQTT服务状态。")
        self.mqtt.publish(topic, payload)

    def dispatch_rpa(self, action_type: str, action_data: Dict[str, Any]) -> str:
        """
        发送RPA操作到专用MQTT主题。
        返回操作提交结果字符串。
        """
        topic = f"msg/{self.user.account}/other_rpa_action"
        payload = {
            "create_time": int(time.time()),
            "action_type": action_type,
            "action_data": action_data,
        }
        self.dispatch(topic, payload)
        return f"RPA操作 '{str(action_type)}' 已成功提交。"
