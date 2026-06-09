"""Command dispatchers used by the MCP server."""

from __future__ import annotations

import time
from queue import Empty, Queue
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

    def dispatch_wait(
        self,
        topic: str,
        payload: Dict[str, Any],
        timeout: float = 0,
    ) -> Dict[str, Any]:
        """Dispatch a message and optionally wait for local RPA execution status."""
        action = self._message_action(payload)
        return self._dispatch_action_wait(action, timeout)

    def dispatch_rpa_wait(
        self,
        action_type: str,
        action_data: Dict[str, Any],
        timeout: float = 0,
    ) -> Dict[str, Any]:
        """Dispatch a direct RPA action and optionally wait for local execution status."""
        action = self._rpa_action(action_type, action_data)
        return self._dispatch_action_wait(action, timeout)

    def _dispatch_action_wait(self, action: Any, timeout: float = 0) -> Dict[str, Any]:
        result_queue = Queue(maxsize=1) if timeout and timeout > 0 else None
        if result_queue is not None and hasattr(action, "result_queue"):
            action.result_queue = result_queue
        self._queue_action(action)
        response: Dict[str, Any] = {
            "status": "queued",
            "queued": True,
            "completed": False,
            "dispatcher": type(self).__name__,
            "action_type": getattr(getattr(action, "action_type", None), "value", ""),
            "queue_size": self._queue_size(),
        }
        if result_queue is None:
            return response
        try:
            result = result_queue.get(timeout=max(0.0, float(timeout)))
        except Empty:
            response["status"] = "timeout"
            response["timeout_seconds"] = timeout
            return response
        ok = bool(result.get("ok"))
        response.update(
            {
                "status": "executed" if ok else "failed",
                "completed": True,
                "result": result,
            }
        )
        return response

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
            from wechat_ai_bot.rpa.action_handlers import SendFileAction

            return SendFileAction(
                file_path=str(
                    payload.get("file_path")
                    or payload.get("path")
                    or payload.get("file_url")
                    or payload.get("message_content")
                    or ""
                ),
                target=str(payload.get("nickname") or payload.get("target") or ""),
                is_chatroom=bool(payload.get("is_chatroom")),
            )
        if local_type == MessageType.Pat:
            from wechat_ai_bot.rpa.action_handlers import PatAction

            target = str(
                payload.get("nickname")
                or payload.get("target")
                or payload.get("room_name")
                or payload.get("user_name")
                or ""
            )
            return PatAction(
                target=target,
                user_name=str(payload.get("user_name") or target),
                is_chatroom=bool(payload.get("is_chatroom") or payload.get("room_name")),
            )
        raise NotImplementedError(f"Unsupported MCP message local_type: {local_type!r}")

    def _rpa_action(self, action_type: str, action_data: Dict[str, Any]) -> Any:
        action_data = action_data or {}
        if isinstance(action_type, RPAActionType):
            action_type_value = action_type.value
        else:
            action_type_value = str(action_type)

        def value(*keys: str) -> str:
            for key in keys:
                item = action_data.get(key)
                if item is not None and str(item) != "":
                    return str(item)
            return ""

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
        if action_type_value in (RPAActionType.SEND_FILE.value, "send_file_msg"):
            from wechat_ai_bot.rpa.action_handlers import SendFileAction

            target = value("target", "nickname", "recipient_name", "room_name")
            return SendFileAction(
                file_path=value("file_path", "path", "file_url", "url"),
                target=target,
                is_chatroom=bool(
                    action_data.get("is_chatroom")
                    or action_data.get("room_name")
                    or target.endswith("@chatroom")
                ),
            )
        if action_type_value in (RPAActionType.PAT.value, "send_pat_msg"):
            from wechat_ai_bot.rpa.action_handlers import PatAction

            target = value("target", "room_name", "nickname", "recipient_name", "user_name")
            user_name = value("user_name", "member_name") or target
            return PatAction(
                target=target,
                user_name=user_name,
                is_chatroom=bool(
                    action_data.get("is_chatroom")
                    or action_data.get("room_name")
                    or target.endswith("@chatroom")
                ),
            )
        if action_type_value in (
            RPAActionType.REMOVE_ROOM_MEMBER.value,
            "remove_room_member",
        ):
            from wechat_ai_bot.rpa.action_handlers import RemoveRoomMemberAction

            return RemoveRoomMemberAction(
                target=value("target", "room_name"),
                user_name=value("user_name", "member_name"),
            )
        if action_type_value in (
            RPAActionType.INVITE_2_ROOM.value,
            "invite_2_room",
            "invite_room_member",
        ):
            from wechat_ai_bot.rpa.action_handlers import Invite2RoomAction

            return Invite2RoomAction(
                target=value("target", "room_name"),
                user_name=value("user_name", "member_name", "contact_name"),
            )
        if action_type_value in (
            RPAActionType.RENAME_ROOM_NAME.value,
            "rename_room_name",
        ):
            from wechat_ai_bot.rpa.action_handlers import RenameRoomNameAction

            return RenameRoomNameAction(
                target=value("target", "room_name"),
                name=value("name", "new_name"),
            )
        if action_type_value in (
            RPAActionType.RENAME_NAME_IN_ROOM.value,
            "rename_name_in_room",
        ):
            from wechat_ai_bot.rpa.action_handlers import RenameNameInRoomAction

            return RenameNameInRoomAction(
                target=value("target", "room_name"),
                name=value("name", "new_name_in_room", "new_name"),
            )

        raise NotImplementedError(
            f"RPA action '{action_type_value}' has not been ported to Linux RPA yet."
        )

    def _queue_action(self, action: Any) -> None:
        queue = getattr(self.bot, "rpa_task_queue", None)
        if queue is None:
            raise RuntimeError("RPA task queue is unavailable.")
        queue.put(action)

    def _queue_size(self) -> int:
        queue = getattr(self.bot, "rpa_task_queue", None)
        if queue is None:
            return -1
        try:
            return int(queue.qsize())
        except Exception:
            return -1


class UnavailableCommandDispatcher:
    """Dispatcher used when neither local bot nor MQTT forwarding is available."""

    def __init__(self, reason: str):
        self.reason = reason

    def dispatch(self, topic: str, payload: Dict[str, Any]) -> None:
        raise ConnectionError(self.reason)

    def dispatch_wait(
        self,
        topic: str,
        payload: Dict[str, Any],
        timeout: float = 0,
    ) -> Dict[str, Any]:
        raise ConnectionError(self.reason)

    def dispatch_rpa(self, action_type: str, action_data: Dict[str, Any]) -> str:
        raise ConnectionError(self.reason)

    def dispatch_rpa_wait(
        self,
        action_type: str,
        action_data: Dict[str, Any],
        timeout: float = 0,
    ) -> Dict[str, Any]:
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

    def dispatch_wait(
        self,
        topic: str,
        payload: Dict[str, Any],
        timeout: float = 0,
    ) -> Dict[str, Any]:
        self.dispatch(topic, payload)
        return {
            "status": "queued",
            "queued": True,
            "completed": False,
            "dispatcher": type(self).__name__,
            "wait_supported": False,
            "message": "MQTT dispatcher cannot wait for local RPA completion.",
        }

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

    def dispatch_rpa_wait(
        self,
        action_type: str,
        action_data: Dict[str, Any],
        timeout: float = 0,
    ) -> Dict[str, Any]:
        message = self.dispatch_rpa(action_type, action_data)
        return {
            "status": "queued",
            "queued": True,
            "completed": False,
            "dispatcher": type(self).__name__,
            "wait_supported": False,
            "message": message,
        }
