"""
Core RPA action types — extracted to avoid circular imports.
Import RPAAction and RPAActionType from here instead of action_handlers.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from queue import Queue
from typing import Any, Dict


class RPAActionType(Enum):
    """RPA operation type enum."""

    SEND_MESSAGE = "send_message"
    SWITCH_CONVERSATION = "switch_conversation"
    QUOTE_MESSAGE = "quote_message"
    MENTION_USER = "mention_user"
    SEND_IMAGE = "send_image"
    SEND_FILE = "send_file"
    SEND_EMOJI = "send_emoji"
    SEND_LINK = "send_link"
    SEND_VIDEO = "send_video"
    SEND_VOICE = "send_voice"
    FORWARD_MESSAGE = "forward_message"
    DOWNLOAD_IMAGE = "download_image"
    DOWNLOAD_VIDEO = "download_video"
    DOWNLOAD_FILE = "download_file"
    PAT = "pat"
    NEW_FRIEND = "new_friend"
    INVITE_2_ROOM = "invice_2_room"
    REMOVE_ROOM_MEMBER = "remove_room_member"
    SEND_PYQ = "send_pyq"
    PUBLIC_ROOM_ANNOUNCEMENT = "public_room_announcement"
    RENAME_ROOM_NAME = "rename_room_name"
    RENAME_ROOM_REMARK = "rename_room_remark"
    RENAME_NAME_IN_ROOM = "rename_name_in_room"
    LEAVE_ROOM = "leave_room"
    SEND_TEXT_MESSAGE = "send_text_message"


@dataclass
class RPAAction:
    """Base class for RPA operations."""

    action_type: RPAActionType = field(default=None, init=False)
    timestamp: datetime = field(default_factory=datetime.now, init=False)
    is_send_message: bool = field(default=None, init=False)
    result_queue: Queue | None = field(
        default=None,
        repr=False,
        compare=False,
        kw_only=True,
    )

    def to_dict(self) -> Dict[str, Any]:
        action_type_value = getattr(self, "action_type", None)
        if action_type_value is None:
            raise NotImplementedError(
                f"{self.__class__.__name__} is missing action_type."
            )
        return {
            "action_type": action_type_value.value,
            "timestamp": self.timestamp.isoformat(),
        }
