#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MCP Server for WeChat-AI Bot (Linux).
Adapted from omni-bot-sdk: removed DatabaseService dependency.
Contact lookups use config-driven data for MVP.
"""

import functools
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Any, Callable, List, Literal, Optional, Tuple

from mcp.server.fastmcp import Context, FastMCP
from wechat_ai_bot.clients.mqtt_client import MQTTClient
from wechat_ai_bot.mcp.debug import register_debug_routes
from wechat_ai_bot.mcp.dispatchers import MqttCommandDispatcher
from wechat_ai_bot.mcp.protocols import CommandDispatcher
from wechat_ai_bot.models import Contact, UserInfo
from wechat_ai_bot.rpa.rpa_action import RPAActionType
from wechat_ai_bot.weixin.message_classes import MessageType

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    """Application context for FastMCP lifespan."""

    userinfo: UserInfo
    command_dispatcher: CommandDispatcher
    contacts: dict  # MVP: config-driven contact list
    rooms: dict     # MVP: config-driven room list


def init_mqtt_client(config: dict) -> MQTTClient:
    """Initialize and connect MQTT client."""
    client_id = "mcp-client"
    client = MQTTClient(
        host=config.get("host", "127.0.0.1"),
        port=config.get("port", 1883),
        client_id=client_id,
        username=config.get("username", "weixin"),
        password=config.get("password", "123456"),
    )
    try:
        client.connect()
        logger.info("MQTT client connected.")
    except Exception as e:
        logger.error(f"MQTT connection failed: {e}")
    return client


@asynccontextmanager
async def app_lifespan(
    app: FastMCP, user_info: UserInfo, mqtt_config: dict
) -> AsyncIterator[AppContext]:
    """Manage application lifecycle."""
    logger.info("MCP app lifespan starting...")

    mqtt_client = init_mqtt_client(mqtt_config)
    dispatcher = MqttCommandDispatcher(mqtt_client, user_info)

    # MVP: empty contact/room dicts (will be populated by RPA discovery)
    app_context = AppContext(
        userinfo=user_info,
        command_dispatcher=dispatcher,
        contacts={},
        rooms={},
    )

    try:
        yield app_context
    finally:
        logger.warning("MCP app shutting down...")
        if hasattr(app_context.command_dispatcher, "mqtt"):
            app_context.command_dispatcher.mqtt.disconnect()
        logger.info("MCP app shutdown complete.")


def handle_tool_exceptions(func: Callable) -> Callable:
    """Decorator for tool function exception handling."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_msg = f"Tool error: {type(e).__name__}: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return error_msg

    return wrapper


def _get_app_context_from_request(ctx: Context) -> AppContext:
    """Get AppContext from request context."""
    if not hasattr(ctx.request_context, "lifespan_context"):
        raise RuntimeError("AppContext not initialized in lifespan.")
    return ctx.request_context.lifespan_context


def _get_nested_config(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        if key in config:
            return config.get(key, default)
        value = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    if hasattr(config, "get"):
        try:
            return config.get(key, default)
        except TypeError:
            return default
    return default


def create_app(user_info: UserInfo, config: dict, bot: Any = None) -> FastMCP:
    """Create and configure the MCP application (Linux port)."""

    lifespan_handler = functools.partial(
        app_lifespan, user_info=user_info, mqtt_config=config.get("mqtt", {})
    )
    mcp_config = config.get("mcp", {})
    mcp_port = int(os.environ.get("MCP_PORT") or mcp_config.get("port", 8000))
    mcp = FastMCP(
        name="WeChat-AI-MCP",
        instructions="""
        You are a WeChat assistant powered by WeChat-AI Bot (Linux).
        - Send text messages via RPA automation.
        - Manage group chats and members.
        Note: Message query is limited without direct DB access.
        Use exact nicknames or WeChat IDs when calling tools.
        """,
        lifespan=lifespan_handler,
        json_response=True,
        host=mcp_config.get("host", "0.0.0.0"),
        port=mcp_port,
    )

    if bot is not None and _get_nested_config(config, "debug.enabled", True):
        register_debug_routes(mcp, bot, config)

    # --- Tool Functions ---

    @mcp.tool()
    @handle_tool_exceptions
    def get_timestamp(ctx: Context) -> int:
        """Get current Unix timestamp in milliseconds."""
        return int(time.time() * 1000)

    @mcp.tool()
    @handle_tool_exceptions
    def get_wechat_user_info(ctx: Context) -> str:
        """Get current WeChat user info (sensitive fields masked)."""
        app_context = _get_app_context_from_request(ctx)
        user_info_dict = asdict(app_context.userinfo)
        for key in ["alias", "account", "phone", "data_dir", "key"]:
            if user_info_dict.get(key) and len(str(user_info_dict[key])) > 1:
                user_info_dict[key] = str(user_info_dict[key])[0] + "*" * (
                    len(str(user_info_dict[key])) - 1
                )
        user_info_dict["db_keys"] = {}
        return json.dumps(user_info_dict, ensure_ascii=False, indent=4)

    @mcp.tool()
    @handle_tool_exceptions
    def query_wechat_msg(
        ctx: Context,
        contact_name: str,
        query: Optional[str] = None,
        start_timestamp: Optional[int] = None,
        end_timestamp: Optional[int] = None,
        limit: Optional[int] = 500,
    ) -> str:
        """
        Query WeChat message history.
        NOTE: On Linux MVP, direct DB access is not available.
        This will be supported in a future update with DB-level reading.
        """
        return json.dumps({
            "status": "unavailable",
            "message": "Direct message query is not available on Linux MVP. "
                       "Visual message reading only supports real-time messages from the active chat window. "
                       "Historical message queries will be supported when Linux WeChat DB access is implemented.",
            "suggestion": "Use the visual message polling feature for real-time message detection.",
        }, ensure_ascii=False)

    @mcp.tool()
    @handle_tool_exceptions
    def send_text_msg(
        ctx: Context,
        recipient_name: str,
        message: str,
        at_user_name: Optional[str] = None,
    ) -> str:
        """
        Send a text message to a user or group via RPA.
        Supports @mention in groups.
        """
        app_context = _get_app_context_from_request(ctx)

        # Simple heuristic: if name contains "群" or "group", treat as chatroom
        is_chatroom = "群" in recipient_name or "group" in recipient_name.lower()
        # Generate a simple username hash
        username = hashlib.md5(recipient_name.encode()).hexdigest()
        if is_chatroom:
            username += "@chatroom"

        topic = f"msg/{app_context.userinfo.account}/rpa_action"
        payload = {
            "local_type": MessageType.Text,
            "message_content": message,
            "username": username,
            "nickname": recipient_name,
            "at_list": [at_user_name] if at_user_name else [],
            "is_chatroom": is_chatroom,
            "create_time": int(time.time()),
        }
        app_context.command_dispatcher.dispatch(topic, payload)
        return f"Message submitted to '{recipient_name}'."

    @mcp.tool()
    @handle_tool_exceptions
    def send_file_msg(ctx: Context, recipient_name: str, file_path: str) -> str:
        """Send a file to a user or group via RPA."""
        app_context = _get_app_context_from_request(ctx)

        is_chatroom = "群" in recipient_name
        username = hashlib.md5(recipient_name.encode()).hexdigest()
        if is_chatroom:
            username += "@chatroom"

        topic = f"msg/{app_context.userinfo.account}/rpa_action"
        payload = {
            "local_type": MessageType.File,
            "message_content": "",
            "username": username,
            "nickname": recipient_name,
            "file": file_path,
            "is_chatroom": is_chatroom,
            "create_time": int(time.time()),
        }
        app_context.command_dispatcher.dispatch(topic, payload)
        return f"File send task submitted to '{recipient_name}'."

    @mcp.tool()
    @handle_tool_exceptions
    def send_pat_msg(
        ctx: Context, user_name: str, room_name: Optional[str] = None
    ) -> str:
        """Send a 'pat' (拍一拍) message."""
        app_context = _get_app_context_from_request(ctx)

        if room_name:
            target = room_name
            is_chatroom = True
        else:
            target = user_name
            is_chatroom = False

        username = hashlib.md5(target.encode()).hexdigest()
        if is_chatroom:
            username += "@chatroom"

        topic = f"msg/{app_context.userinfo.account}/rpa_action"
        payload = {
            "local_type": MessageType.Pat,
            "message_content": "",
            "username": username,
            "nickname": target,
            "at_list": [user_name],
            "is_chatroom": is_chatroom,
            "create_time": int(time.time()),
        }
        app_context.command_dispatcher.dispatch(topic, payload)
        return f"Pat message submitted to '{user_name}'."

    @mcp.tool()
    @handle_tool_exceptions
    def leave_room(ctx: Context, room_name: str) -> str:
        """Leave a group chat."""
        app_context = _get_app_context_from_request(ctx)
        return app_context.command_dispatcher.dispatch_rpa(
            RPAActionType.LEAVE_ROOM.value, {"target": room_name}
        )

    @mcp.tool()
    @handle_tool_exceptions
    def public_room_announcement(
        ctx: Context, room_name: str, content: str, force_edit: bool = False
    ) -> str:
        """Publish or edit a group announcement."""
        app_context = _get_app_context_from_request(ctx)
        return app_context.command_dispatcher.dispatch_rpa(
            RPAActionType.PUBLIC_ROOM_ANNOUNCEMENT.value,
            {"target": room_name, "content": content, "force_edit": force_edit},
        )

    @mcp.tool()
    @handle_tool_exceptions
    def query_room_member_list(ctx: Context, room_name: str) -> str:
        """Query group member list. Limited on Linux MVP."""
        return json.dumps({
            "status": "limited",
            "message": "Room member list query is limited on Linux MVP without DB access. "
                       "Use RPA-based visual scanning for member discovery.",
            "room": room_name,
        }, ensure_ascii=False)

    @mcp.tool()
    @handle_tool_exceptions
    def remove_room_member(ctx: Context, room_name: str, member_name: str) -> str:
        """Remove a member from a group chat."""
        app_context = _get_app_context_from_request(ctx)
        return app_context.command_dispatcher.dispatch_rpa(
            RPAActionType.REMOVE_ROOM_MEMBER.value,
            {"target": room_name, "user_name": member_name},
        )

    @mcp.tool()
    @handle_tool_exceptions
    def invite_room_member(ctx: Context, room_name: str, user_name: str) -> str:
        """Invite a user to a group chat."""
        app_context = _get_app_context_from_request(ctx)
        return app_context.command_dispatcher.dispatch_rpa(
            RPAActionType.INVITE_2_ROOM.value,
            {"target": room_name, "user_name": user_name},
        )

    @mcp.tool()
    @handle_tool_exceptions
    def rename_room_name(ctx: Context, room_name: str, new_name: str) -> str:
        """Rename a group chat."""
        app_context = _get_app_context_from_request(ctx)
        return app_context.command_dispatcher.dispatch_rpa(
            RPAActionType.RENAME_ROOM_NAME.value,
            {"target": room_name, "name": new_name},
        )

    @mcp.tool()
    @handle_tool_exceptions
    def rename_name_in_room(ctx: Context, room_name: str, new_name_in_room: str) -> str:
        """Change your nickname in a group chat."""
        app_context = _get_app_context_from_request(ctx)
        return app_context.command_dispatcher.dispatch_rpa(
            RPAActionType.RENAME_NAME_IN_ROOM.value,
            {"target": room_name, "name": new_name_in_room},
        )

    return mcp
