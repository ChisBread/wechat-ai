# mcp/protocols.py
from typing import Any, Dict, Protocol


class CommandDispatcher(Protocol):
    """
    命令分发器协议。
    统一定义消息分发接口，便于不同实现（如MQTT、队列等）互换。
    """

    def dispatch(self, topic: str, payload: Dict[str, Any]) -> None:
        """
        分发一个通用的消息。
        topic: 目标主题或通道
        payload: 消息内容
        """
        ...

    def dispatch_rpa(self, action_type: str, action_data: Dict[str, Any]) -> str:
        """
        分发一个RPA操作，并返回确认信息。
        action_type: 操作类型
        action_data: 操作参数
        """
        ...

    def dispatch_wait(
        self,
        topic: str,
        payload: Dict[str, Any],
        timeout: float = 0,
    ) -> Dict[str, Any]:
        """
        分发一个通用消息，并在支持时等待本地 RPA 执行结果。
        MQTT 等远程转发实现可以只返回 queued。
        """
        ...
