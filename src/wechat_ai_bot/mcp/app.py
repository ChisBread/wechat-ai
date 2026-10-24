#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MCP Server for WeChat-AI Bot (Linux).
Adapted from omni-bot-sdk for Linux WeChat + SQLCipher DB access.
"""

import functools
import hashlib
import hmac
import json
import logging
import os
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ImageContent, TextContent
from wechat_ai_bot.clients.mqtt_client import MQTTClient
from wechat_ai_bot.mcp.debug import add_media_base64, build_debug_status, register_debug_routes
from wechat_ai_bot.mcp.dispatchers import (
    MqttCommandDispatcher,
    QueueCommandDispatcher,
    UnavailableCommandDispatcher,
)
from wechat_ai_bot.mcp.image_upload import decode_image_upload
from wechat_ai_bot.mcp.protocols import CommandDispatcher
from wechat_ai_bot.models import UserInfo
from wechat_ai_bot.rpa.rpa_action import RPAActionType
from wechat_ai_bot.rpa.voice_audio import prepare_voice
from wechat_ai_bot.weixin.message_content import chat_message_text, contact_in_chat, message_content_text
from wechat_ai_bot.weixin.parser.audio_parser import audio_transcript
from wechat_ai_bot.weixin.message_classes import MessageType

logger = logging.getLogger(__name__)

_MCP_TOKEN_ENV = "WECHAT_AI_MCP_TOKEN"
_DEFAULT_MCP_TOKEN = "wechat"
_MCP_SCOPE = "wechat-ai"


@dataclass
class AppContext:
    """Application context for FastMCP lifespan."""

    userinfo: UserInfo
    command_dispatcher: CommandDispatcher
    contacts: dict  # MVP: config-driven contact list
    rooms: dict     # MVP: config-driven room list


def init_mqtt_client(config: dict) -> MQTTClient:
    """Initialize and connect MQTT client."""
    if not config.get("host"):
        raise RuntimeError("MQTT host is not configured.")
    client_id = "mcp-client"
    client = MQTTClient(
        host=config.get("host"),
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
    app: FastMCP,
    user_info: UserInfo,
    mqtt_config: dict,
    bot: Any = None,
) -> AsyncIterator[AppContext]:
    """Manage application lifecycle."""
    logger.info("MCP app lifespan starting...")

    mqtt_client = None
    if bot is not None and getattr(bot, "rpa_task_queue", None) is not None:
        dispatcher: CommandDispatcher = QueueCommandDispatcher(bot, user_info)
        logger.info("MCP command dispatcher: local RPA queue")
    elif mqtt_config.get("host"):
        mqtt_client = init_mqtt_client(mqtt_config)
        dispatcher = MqttCommandDispatcher(mqtt_client, user_info)
        logger.info("MCP command dispatcher: MQTT")
    else:
        dispatcher = UnavailableCommandDispatcher(
            "No local bot RPA queue is available and mqtt.host is empty."
        )
        logger.warning("MCP command dispatcher unavailable.")

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
        if mqtt_client is not None:
            mqtt_client.disconnect()
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


class EnvMcpTokenVerifier:
    """Validate the MCP Bearer token from environment variables."""

    async def verify_token(self, token: str) -> Any | None:
        from mcp.server.auth.provider import AccessToken

        expected = str(os.environ.get(_MCP_TOKEN_ENV, _DEFAULT_MCP_TOKEN) or "")
        if not expected or expected == _DEFAULT_MCP_TOKEN:
            return None
        if not hmac.compare_digest(str(token or "").encode("utf-8"), expected.encode("utf-8")):
            return None
        return AccessToken(
            token="env-token",
            client_id="wechat-ai-local",
            scopes=[_MCP_SCOPE],
            subject="local-agent",
        )


def _mcp_auth_settings(config: Any) -> Any:
    from mcp.server.auth.settings import AuthSettings

    issuer_url = (
        os.environ.get("WECHAT_AI_MCP_ISSUER_URL")
        or _get_nested_config(config, "mcp.auth_issuer_url", "http://localhost:8000")
    )
    return AuthSettings(
        issuer_url=issuer_url,
        required_scopes=[_MCP_SCOPE],
        resource_server_url=None,
    )


def create_app(user_info: UserInfo, config: dict, bot: Any = None) -> FastMCP:
    """Create and configure the MCP application (Linux port)."""

    lifespan_handler = functools.partial(
        app_lifespan,
        user_info=user_info,
        mqtt_config=config.get("mqtt", {}),
        bot=bot,
    )
    mcp_config = config.get("mcp", {})
    mcp_port = int(os.environ.get("MCP_PORT") or mcp_config.get("port", 8000))
    mcp = FastMCP(
        name="WeChat-AI-MCP",
        instructions="""
        You are a WeChat assistant powered by WeChat-AI Bot (Linux).
        - Query contacts and message history through read-only Linux WeChat SQLCipher DB access.
        - Send text, files, pat messages, and supported group actions via the local RPA queue only when WECHAT_AI_MCP_WRITE_ENABLED=true.
        - Use send_image_msg to upload base64 images (PNG/JPEG/WebP/GIF, up to 10 MiB) and send via the local file RPA action.
        - Admin tools that mutate local runtime state require WECHAT_AI_MCP_ADMIN_ENABLED=true.
        - Use dry_run=true before sending or changing group state when the tool supports it.
        - High-impact group operations require explicit human confirmation.
        - Treat status="unavailable" as a hard failure for tools that are not yet ported.
        Use exact nicknames or WeChat IDs when calling tools.
        """,
        lifespan=lifespan_handler,
        json_response=True,
        host=mcp_config.get("host", "0.0.0.0"),
        port=mcp_port,
        token_verifier=EnvMcpTokenVerifier(),
        auth=_mcp_auth_settings(config),
    )

    if bot is not None and _get_nested_config(config, "debug.enabled", True):
        register_debug_routes(mcp, bot, config)

    # --- Tool Functions ---

    def _database_service() -> Any:
        return getattr(bot, "database_service", None) if bot is not None else None

    def _message_service() -> Any:
        return getattr(bot, "message_service", None) if bot is not None else None

    def _window_manager() -> Any:
        return getattr(bot, "window_manager", None) if bot is not None else None

    def _message_factory_service() -> Any:
        return getattr(bot, "message_factory_service", None) if bot is not None else None

    def _json(payload: Any, *, indent: Optional[int] = None) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=indent)

    def _media_tool_result(payload: dict[str, Any]) -> str | list[TextContent | ImageContent]:
        """Keep metadata in the first text block; expose pixels as image blocks."""
        images = []
        for message in payload.get("messages", []):
            encoded = message.get("base64")
            mime = message.get("base64_mime_type", "")
            if encoded and mime.startswith("image/"):
                # Do not also put image bytes in model-visible JSON text.
                message.pop("base64")
                message["image_content_index"] = len(images) + 1
                images.append(ImageContent(type="image", data=encoded, mimeType=mime))
            quote_encoded = message.pop("quote_base64", None)
            quote_mime = message.get("quote_base64_mime_type", "")
            if quote_encoded and quote_mime.startswith("image/"):
                message["quote_image_content_index"] = len(images) + 1
                images.append(ImageContent(type="image", data=quote_encoded, mimeType=quote_mime))
        payload["image_count"] = len(images)
        metadata = _json(payload)
        return [TextContent(type="text", text=metadata), *images] if images else metadata

    def _unavailable_tool(tool_name: str, detail: str) -> str:
        return _json(
            {
                "status": "unavailable",
                "tool": tool_name,
                "message": detail,
            }
        )

    def _database_unavailable_payload(db: Any = None) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "message": "Linux WeChat database service is not available.",
            "detail": getattr(db, "last_error", "") if db else "db service missing",
        }

    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(number, maximum))

    def _env_flag(name: str, default: bool = False) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def _mcp_write_enabled() -> bool:
        return _env_flag("WECHAT_AI_MCP_WRITE_ENABLED", False)

    def _mcp_admin_enabled() -> bool:
        return _env_flag("WECHAT_AI_MCP_ADMIN_ENABLED", False)

    def _mcp_write_blocked(tool_name: str) -> str:
        return _json(
            {
                "status": "blocked",
                "tool": tool_name,
                "message": "MCP write tools are disabled. Set WECHAT_AI_MCP_WRITE_ENABLED=true to allow this operation.",
            }
        )

    def _mcp_admin_blocked(tool_name: str) -> str:
        return _json(
            {
                "status": "blocked",
                "tool": tool_name,
                "message": "MCP admin tools are disabled. Set WECHAT_AI_MCP_ADMIN_ENABLED=true to allow this operation.",
            }
        )

    def _mcp_confirmation_required(tool_name: str, message: str) -> str:
        return _json(
            {
                "status": "confirmation_required",
                "tool": tool_name,
                "message": message,
                "required_argument": "confirm=true",
            }
        )

    def _resolve_rpa_target(name: str) -> dict[str, Any]:
        raw_name = str(name or "").strip()
        contact = _resolve_contact_name(raw_name)
        if contact:
            username = str(getattr(contact, "username", "") or "")
            display_name = str(getattr(contact, "display_name", "") or username)
            is_chatroom = username.endswith("@chatroom")
            return {
                "input": raw_name,
                "username": username,
                "display_name": display_name,
                "target": display_name,
                "is_chatroom": is_chatroom,
                "contact": _contact_payload(contact),
            }
        return {
            "error": "contact_not_found",
            "input": raw_name,
            "message": "Contact or group not found after refreshing contact metadata",
        }

    def _rpa_target_not_found(resolved: dict[str, Any]) -> str:
        return _json({
            "status": "not_found",
            "error": resolved["error"],
            "message": resolved["message"],
            "resolved": resolved,
            "queued": False,
            "completed": False,
        })

    def _dispatch_rpa_tool(
        ctx: Context,
        *,
        tool_name: str,
        action_type: str,
        action_data: dict[str, Any],
        dry_run: bool,
        wait_seconds: Optional[float],
        extra: Optional[dict[str, Any]] = None,
        on_complete: Optional[Callable[[], None]] = None,
        on_queued: Optional[Callable[[], None]] = None,
    ) -> str:
        action_type_value = action_type.value if isinstance(action_type, RPAActionType) else str(action_type)
        response: dict[str, Any] = {
            "status": "dry_run" if dry_run else "queued",
            "tool": tool_name,
            "action_type": action_type_value,
            "action_data": action_data,
        }
        if extra:
            response.update(extra)
        if dry_run:
            return _json(response)
        if not _mcp_write_enabled():
            return _mcp_write_blocked(tool_name)
        app_context = _get_app_context_from_request(ctx)
        wait_timeout = _bounded_float(wait_seconds, 12, 0, 60)
        lifecycle = {}
        if on_complete is not None:
            if not isinstance(app_context.command_dispatcher, QueueCommandDispatcher):
                response.update(
                    status="unavailable", queued=False, completed=False,
                    message="Staged media requires the local RPA queue dispatcher",
                )
                return _json(response)
            lifecycle = {"on_complete": on_complete, "on_queued": on_queued}
        dispatch_rpa_wait = getattr(app_context.command_dispatcher, "dispatch_rpa_wait", None)
        if callable(dispatch_rpa_wait):
            dispatch_result = dispatch_rpa_wait(
                action_type_value,
                action_data,
                timeout=wait_timeout,
                **lifecycle,
            )
        else:
            message = app_context.command_dispatcher.dispatch_rpa(action_type_value, action_data)
            dispatch_result = {
                "status": "queued",
                "queued": True,
                "completed": False,
                "dispatcher": type(app_context.command_dispatcher).__name__,
                "message": message,
            }
        response.update(dispatch_result)
        response["dispatcher"] = type(app_context.command_dispatcher).__name__
        response["wait_seconds"] = wait_timeout
        return _json(response)

    def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(number, maximum))

    def _resolve_contact_name(name: str) -> Any:
        db = _database_service()
        if not db or not getattr(db, "is_available", False):
            return None
        contact = db.ensure_contact(name)
        if contact:
            return contact
        db.refresh_contact_metadata()
        matches = db.get_contact_by_display_name(name)
        return matches[0] if matches else None

    def _contact_payload(contact: Any) -> dict[str, Any]:
        if not contact:
            return {}
        username = str(getattr(contact, "username", "") or "")
        return {
            "id": getattr(contact, "id", None),
            "username": username,
            "display_name": str(getattr(contact, "display_name", "") or username),
            "remark": str(getattr(contact, "remark", "") or ""),
            "nick_name": str(getattr(contact, "nick_name", "") or ""),
            "alias": str(getattr(contact, "alias", "") or ""),
            "is_chatroom": bool(getattr(contact, "is_chatroom", False)),
            "local_type": getattr(contact, "local_type", None),
        }

    def _contact_detail_payload(db: Any, contact: Any) -> dict[str, Any]:
        payload = _contact_payload(contact)
        if not contact:
            return payload
        username = str(getattr(contact, "username", "") or "")
        payload.update(
            {
                "delete_flag": getattr(contact, "delete_flag", None),
                "verify_flag": getattr(contact, "verify_flag", None),
                "chat_room_notify": getattr(contact, "chat_room_notify", None),
                "head_img_md5": str(getattr(contact, "head_img_md5", "") or ""),
                "description": str(getattr(contact, "description", "") or ""),
                "has_message_table": bool(username in (getattr(db, "_message_username_map", {}) or {})),
            }
        )
        if payload.get("is_chatroom"):
            try:
                payload["member_count"] = len(db.get_room_member_list(username))
            except Exception:
                payload["member_count"] = None
        return payload

    def _search_contacts(db: Any, query: str, limit: int) -> list[Any]:
        if not db or not query:
            return []
        results: list[Any] = []
        direct = db.get_contact_by_username(query)
        if direct:
            results.append(direct)
        try:
            results.extend(db.get_contact_by_display_name(query))
        except Exception:
            pass

        needle = query.lower()
        cache = getattr(db, "_contact_by_username", {}) or {}
        for contact in cache.values():
            fields = [
                getattr(contact, "username", ""),
                getattr(contact, "display_name", ""),
                getattr(contact, "remark", ""),
                getattr(contact, "nick_name", ""),
                getattr(contact, "alias", ""),
            ]
            if any(needle in str(value).lower() for value in fields if value):
                results.append(contact)

        deduped: list[Any] = []
        seen: set[str] = set()
        for contact in results:
            username = str(getattr(contact, "username", "") or "")
            if not username or username in seen:
                continue
            seen.add(username)
            deduped.append(contact)
            if len(deduped) >= limit:
                break
        return deduped

    def _message_type_name(local_type: Any) -> str:
        try:
            return MessageType.name(local_type)
        except Exception:
            return str(local_type or "")

    def _format_timestamp(value: Any) -> str:
        try:
            timestamp = int(value or 0)
        except (TypeError, ValueError):
            return ""
        if timestamp <= 0:
            return ""
        if timestamp > 10_000_000_000:
            timestamp = timestamp // 1000
        try:
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""

    def _truncate_text(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."

    def _message_content_text(value: Any, ct_flag: Any, limit: int, *, chat_username: str = "", sender_username: str = "") -> str:
        return _truncate_text(chat_message_text(value, ct_flag, chat_username=chat_username, sender_username=sender_username), limit)

    def _unpack_text_search_row(row: tuple) -> tuple[Any, str, str, Any, Any, Any]:
        content = row[0] if len(row) > 0 else ""
        sender_username = row[1] if len(row) > 1 else ""
        db_path = row[2] if len(row) > 2 else ""
        create_time = row[3] if len(row) > 3 else None
        server_id = row[4] if len(row) > 4 else ""
        ct_flag = row[5] if len(row) > 5 else None
        return content, str(sender_username or ""), str(db_path or ""), create_time, server_id, ct_flag

    def _message_payload(db: Any, row: tuple, *, include_text: bool = True, chat_username: str = "") -> dict[str, Any]:
        local_type = row[2] if len(row) > 2 else None
        sender = None
        sender_id = row[4] if len(row) > 4 else None
        try:
            sender = db.get_contact_by_sender_id(sender_id, row[17] if len(row) > 17 else None)
        except Exception:
            sender = None
        sender = contact_in_chat(db, sender, chat_username)
        payload: dict[str, Any] = {
            "local_id": row[0] if len(row) > 0 else None,
            "server_id": str(row[1] if len(row) > 1 else ""),
            "type": local_type,
            "type_name": _message_type_name(local_type),
            "sort_seq": row[3] if len(row) > 3 else None,
            "sender_username": getattr(sender, "username", "") if sender else "",
            "sender_display": getattr(sender, "display_name", "") if sender else "",
            "create_time": row[5] if len(row) > 5 else None,
            "time": _format_timestamp(row[5] if len(row) > 5 else None),
        }
        if include_text and local_type in (MessageType.Text, MessageType.Text2):
            payload["text"] = _message_content_text(
                row[12] if len(row) > 12 else "",
                row[15] if len(row) > 15 else None,
                4000,
                chat_username=chat_username,
                sender_username=getattr(sender, "username", ""),
            )
        elif include_text and local_type == MessageType.Audio:
            transcript = audio_transcript(
                message_content_text(row[12] if len(row) > 12 else "",
                                     row[15] if len(row) > 15 else None),
                row[14] if len(row) > 14 else None,
            )
            payload["voice_to_text"] = transcript
            payload["text"] = f"【语音】{transcript}"
        return payload

    def _rows_for_contact(db: Any, contact: Any, limit: int, order: str = "desc") -> list[tuple]:
        rows = db.get_messages_by_username(contact.username, count=limit, order=order)
        if str(order).lower() != "asc":
            rows.reverse()
        return rows

    def _table_name_for_contact(contact: Any) -> str:
        return f"Msg_{hashlib.md5(str(contact.username).encode()).hexdigest()}"

    def _factory_message_payload(db: Any, contact: Any, row: tuple) -> dict[str, Any]:
        payload = _message_payload(db, row, include_text=True, chat_username=contact.username if contact else "")
        factory_service = _message_factory_service()
        if not factory_service:
            payload["factory_ok"] = False
            payload["factory_error"] = "message factory service unavailable"
            return payload
        try:
            message = factory_service.create_message((_table_name_for_contact(contact), row))
        except Exception as exc:
            payload["factory_ok"] = False
            payload["factory_error"] = f"{type(exc).__name__}: {exc}"
            return payload
        if not message:
            payload["factory_ok"] = False
            payload["factory_error"] = "unsupported message type"
            payload["raw_content"] = _message_content_text(
                row[12] if len(row) > 12 else "",
                row[15] if len(row) > 15 else None,
                1200,
            )
            return payload

        payload["factory_ok"] = True
        try:
            payload["text"] = _truncate_text(str(message.to_text() or ""), 4000)
        except Exception:
            pass
        for key in (
            "path",
            "thumb_path",
            "cover_path",
            "file_name",
            "file_size",
            "file_type",
            "md5",
            "raw_md5",
            "duration",
            "url",
            "description",
        ):
            if not hasattr(message, key):
                continue
            value = getattr(message, key)
            if isinstance(value, (bytes, bytearray)):
                continue
            payload[key] = str(value) if isinstance(value, Path) else value
        add_media_base64(db, payload)
        quote = getattr(message, "quote_message", None)
        if quote is not None:
            quote_type = getattr(quote, "local_type", None)
            quote_contact = getattr(quote, "contact", None)
            quote_text = ""
            try:
                quote_text = _truncate_text(str(quote.to_text() or ""), 2000)
            except Exception:
                pass
            payload["quote"] = {
                "local_id": getattr(quote, "local_id", None),
                "server_id": str(getattr(quote, "server_id", "") or ""),
                "type": quote_type,
                "type_name": _message_type_name(quote_type),
                "sender_username": getattr(quote_contact, "username", "") or "",
                "sender_display": getattr(quote_contact, "display_name", "") or "",
                "text": quote_text,
            }
            if quote_type in (MessageType.Image, MessageType.Emoji):
                try:
                    quote_payload = {"type": quote_type}
                    for key in ("path", "thumb_path", "md5"):
                        if hasattr(quote, key):
                            value = getattr(quote, key)
                            quote_payload[key] = str(value) if isinstance(value, Path) else value
                    add_media_base64(db, quote_payload)
                    quote_encoded = quote_payload.get("base64")
                    quote_mime = quote_payload.get("base64_mime_type", "")
                    if quote_encoded and quote_mime.startswith("image/"):
                        payload["quote_base64"] = quote_encoded
                        payload["quote_base64_mime_type"] = quote_mime
                except Exception:
                    # Keep quote metadata even when the media cannot be decoded.
                    pass
        return payload

    def _window_status_payload(window_manager: Any) -> dict[str, Any]:
        if not window_manager:
            return {"status": "unavailable", "message": "window manager unavailable"}
        refresh = getattr(window_manager, "refresh_window_geometry", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                logger.debug("refresh_window_geometry failed", exc_info=True)
        return {
            "state": getattr(window_manager, "last_window_state", ""),
            "current_window": bool(getattr(window_manager, "current_window", None)),
            "target_size": list(getattr(window_manager, "target_window_size", ()) or ()),
            "actual_geometry": getattr(window_manager, "actual_window_geometry", {}) or {},
            "aligned": bool(getattr(window_manager, "window_aligned", False)),
            "window_id": getattr(window_manager, "window_id", None),
            "message_region": (
                [
                    getattr(window_manager, "MSG_TOP_X", 0),
                    getattr(window_manager, "MSG_TOP_Y", 0),
                    getattr(window_manager, "MSG_WIDTH", 0),
                    getattr(window_manager, "MSG_HEIGHT", 0),
                ]
                if getattr(window_manager, "MSG_WIDTH", 0) and getattr(window_manager, "MSG_HEIGHT", 0)
                else None
            ),
            "sidebar_width": getattr(window_manager, "SIDE_BAR_WIDTH", 0),
            "session_list_width": getattr(window_manager, "SESSION_LIST_WIDTH", 0),
            "title_bar_height": getattr(window_manager, "TITLE_BAR_HEIGHT", 0),
            "send_button": (
                getattr(window_manager, "ICON_CONFIGS", {})
                .get("send_button", {})
                .get("position")
            ),
        }

    def _chat_line(message: dict[str, Any]) -> str:
        sender = message.get("sender_display") or message.get("sender_username") or "unknown"
        when = message.get("time") or str(message.get("create_time") or "")
        text = message.get("text")
        if text:
            body = str(text).replace("\n", " ").strip()
        else:
            body = f"[{message.get('type_name') or message.get('type') or 'message'}]"
        return f"{when} {sender}: {body}".strip()

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
        for key in ["alias", "account", "phone", "data_dir", "key", "dbkey", "dat_key"]:
            if user_info_dict.get(key) and len(str(user_info_dict[key])) > 1:
                user_info_dict[key] = str(user_info_dict[key])[0] + "*" * (
                    len(str(user_info_dict[key])) - 1
                )
        user_info_dict["raw_keys"] = {}
        user_info_dict["db_keys"] = {}
        return json.dumps(user_info_dict, ensure_ascii=False, indent=4)

    @mcp.tool()
    @handle_tool_exceptions
    def get_runtime_status(ctx: Context) -> str:
        """
        Get bot, RPA, database, queue, YOLO, and dashboard status without chat content.
        """
        if bot is None:
            app_context = _get_app_context_from_request(ctx)
            return _json(
                {
                    "status": "limited",
                    "message": "MCP server is running without a bot instance.",
                    "dispatcher": type(app_context.command_dispatcher).__name__,
                }
            )
        status = build_debug_status(bot, config, show_sensitive=False)
        status["mcp"] = {
            "endpoint_container": "http://localhost:8000/mcp",
            "endpoint_host_default": "http://localhost:8100/mcp",
            "dashboard_host_default": "http://localhost:8100/dashboard",
        }
        try:
            app_context = _get_app_context_from_request(ctx)
            status["mcp"]["dispatcher"] = type(app_context.command_dispatcher).__name__
        except Exception:
            pass
        return _json(status)

    @mcp.tool()
    @handle_tool_exceptions
    def get_wechat_window_status(ctx: Context) -> str:
        """
        Get current WeChat X11 window geometry, alignment, layout, and send button state.
        """
        window_manager = _window_manager()
        payload = _window_status_payload(window_manager)
        payload["status"] = "ok" if window_manager else "unavailable"
        return _json(payload)

    @mcp.tool()
    @handle_tool_exceptions
    def reset_wechat_window(ctx: Context) -> str:
        """
        Normalize WeChat window state for visual RPA and YOLO: enter main window, resize, and rebuild layout.
        """
        if not _mcp_admin_enabled():
            return _mcp_admin_blocked("reset_wechat_window")
        window_manager = _window_manager()
        if not window_manager:
            return _json({"status": "unavailable", "message": "window manager unavailable"})
        ensure_ready = getattr(window_manager, "ensure_action_ready", None)
        if not callable(ensure_ready):
            return _json({"status": "unavailable", "message": "window reset is unavailable"})
        ok = bool(ensure_ready())
        if ok and bot is not None and hasattr(bot, "chat_window_ready"):
            bot.chat_window_ready = True
        return _json(
            {
                "status": "ok" if ok else "failed",
                "window": _window_status_payload(window_manager),
            }
        )

    @mcp.tool()
    @handle_tool_exceptions
    def discover_dat_keys(
        ctx: Context,
        send_probe: bool = True,
        scan_timeout_seconds: Optional[float] = 120,
    ) -> str:
        """
        Send a probe image to File Transfer Assistant and try to discover image DAT AES/XOR keys.
        Requires MCP admin; sending the probe also requires MCP write.
        """
        if not _mcp_admin_enabled():
            return _mcp_admin_blocked("discover_dat_keys")
        if send_probe and not _mcp_write_enabled():
            return _mcp_write_blocked("discover_dat_keys")
        if bot is None:
            return _json({"status": "unavailable", "message": "bot instance unavailable"})
        try:
            from wechat_ai_bot.services.core.wechat_dat_key_discovery import (
                WeChatDatKeyDiscovery,
            )

            discovery = WeChatDatKeyDiscovery(
                bot,
                xwechat_root=_get_nested_config(config, "debug.xwechat_files_root", "/config/xwechat_files"),
            )
            result = discovery.discover(
                send_probe=bool(send_probe),
                scan_timeout_seconds=_bounded_float(scan_timeout_seconds, 120, 5, 300),
            ).to_dict()
        except Exception as exc:
            return _json(
                {
                    "status": "error",
                    "tool": "discover_dat_keys",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
        result["tool"] = "discover_dat_keys"
        return _json(result)

    @mcp.tool()
    @handle_tool_exceptions
    def search_contacts(
        ctx: Context,
        query: str,
        limit: Optional[int] = 20,
        include_chatrooms: bool = True,
        include_contacts: bool = True,
    ) -> str:
        """
        Search contacts and chatrooms by WeChat ID, display name, remark, nickname, or alias.
        """
        db = _database_service()
        if not db or not getattr(db, "is_available", False):
            return _json(_database_unavailable_payload(db))
        query = str(query or "").strip()
        if not query:
            return _json({"status": "invalid_request", "message": "query is required"})
        bounded_limit = max(1, min(int(limit or 20), 100))
        contacts = []
        for contact in _search_contacts(db, query, bounded_limit):
            is_chatroom = bool(getattr(contact, "is_chatroom", False))
            if is_chatroom and not include_chatrooms:
                continue
            if not is_chatroom and not include_contacts:
                continue
            contacts.append(contact)
            if len(contacts) >= bounded_limit:
                break
        return _json(
            {
                "status": "ok",
                "query": query,
                "count": len(contacts),
                "contacts": [_contact_payload(contact) for contact in contacts],
            }
        )

    @mcp.tool()
    @handle_tool_exceptions
    def get_contact_detail(ctx: Context, contact_name: str) -> str:
        """
        Resolve one contact/chatroom and return detail useful for deciding follow-up tools.
        """
        db = _database_service()
        if not db or not getattr(db, "is_available", False):
            return _json(_database_unavailable_payload(db))
        contact = _resolve_contact_name(contact_name)
        if not contact:
            return _json(
                {
                    "status": "not_found",
                    "message": f"未找到联系人或群聊: {contact_name}",
                }
            )
        return _json({"status": "ok", "contact": _contact_detail_payload(db, contact)})

    @mcp.tool()
    @handle_tool_exceptions
    def get_database_status(ctx: Context) -> str:
        """Get Linux WeChat database discovery, key, contact, and message-table status."""
        db = _database_service()
        if not db:
            return _json(_database_unavailable_payload(db))
        status = db.get_status() if hasattr(db, "get_status") else {}
        return _json({"status": "ok" if getattr(db, "is_available", False) else "unavailable", "database": status})

    @mcp.tool()
    @handle_tool_exceptions
    def refresh_database(ctx: Context) -> str:
        """Rescan Linux WeChat databases and SQLCipher keys, then reload contact/message maps."""
        if not _mcp_admin_enabled():
            return _mcp_admin_blocked("refresh_database")
        db = _database_service()
        if not db or not hasattr(db, "refresh"):
            return _json(_database_unavailable_payload(db))
        status = db.refresh()
        return _json(
            {
                "status": "ok" if getattr(db, "is_available", False) else "unavailable",
                "database": status,
            }
        )

    @mcp.tool()
    @handle_tool_exceptions
    def set_message_polling(ctx: Context, paused: bool) -> str:
        """Pause or resume database message polling."""
        if not _mcp_admin_enabled():
            return _mcp_admin_blocked("set_message_polling")
        message_service = _message_service()
        if not message_service:
            return _json({"status": "unavailable", "message": "message service unavailable"})
        if paused:
            if not hasattr(message_service, "pause"):
                return _json({"status": "unavailable", "message": "message pause is unavailable"})
            message_service.pause()
        else:
            if not hasattr(message_service, "resume"):
                return _json({"status": "unavailable", "message": "message resume is unavailable"})
            message_service.resume()
        return _json(
            {
                "status": "ok",
                "paused": bool(getattr(message_service, "is_paused", False)),
                "running": bool(getattr(message_service, "is_running", False)),
                "queue_size": getattr(getattr(message_service, "message_queue", None), "qsize", lambda: None)(),
            }
        )

    @mcp.tool()
    @handle_tool_exceptions
    def get_recent_chats(ctx: Context, limit: Optional[int] = 20) -> str:
        """
        List chats that have message tables, ordered by the latest known message time.
        """
        db = _database_service()
        if not db or not getattr(db, "is_available", False):
            return _json(_database_unavailable_payload(db))
        bounded_limit = max(1, min(int(limit or 20), 100))
        username_map = getattr(db, "_message_username_map", {}) or {}
        chats: list[dict[str, Any]] = []
        for username in username_map.keys():
            try:
                rows = db.get_messages_by_username(username, count=1)
            except Exception:
                continue
            if not rows:
                continue
            contact = db.ensure_contact(username)
            row = rows[0]
            chats.append(
                {
                    "contact": _contact_payload(contact) if contact else {"username": username},
                    "last_message": _message_payload(db, row, include_text=True, chat_username=username),
                }
            )
        chats.sort(
            key=lambda item: item.get("last_message", {}).get("create_time") or 0,
            reverse=True,
        )
        return _json({"status": "ok", "count": len(chats[:bounded_limit]), "chats": chats[:bounded_limit]})

    @mcp.tool(structured_output=False)
    @handle_tool_exceptions
    def get_recent_messages(
        ctx: Context,
        contact_name: str,
        limit: Optional[int] = 30,
        include_non_text: bool = True,
        parse_media: bool = False,
    ) -> str | list[TextContent | ImageContent]:
        """
        Get recent messages for a contact/chatroom. Set parse_media=true to view images.
        Returns JSON metadata first, followed by native MCP image blocks for decoded images.
        image_content_index and quote_image_content_index map images to their content blocks.
        """
        db = _database_service()
        if not db or not getattr(db, "is_available", False):
            return _json(_database_unavailable_payload(db))
        contact = _resolve_contact_name(contact_name)
        if not contact:
            return _json(
                {
                    "status": "not_found",
                    "message": f"未找到联系人或群聊: {contact_name}",
                }
            )
        bounded_limit = _bounded_int(limit, 30, 1, 200)
        rows = _rows_for_contact(db, contact, bounded_limit)
        messages = [
            _factory_message_payload(db, contact, row) if parse_media else _message_payload(db, row, include_text=True, chat_username=contact.username if contact else "")
            for row in rows
        ]
        if not include_non_text:
            messages = [
                message
                for message in messages
                if message.get("type") in (MessageType.Text, MessageType.Text2)
            ]
        return (_media_tool_result if parse_media else _json)(
            {
                "status": "ok",
                "contact": _contact_payload(contact),
                "count": len(messages),
                "messages": messages,
            }
        )

    @mcp.tool()
    @handle_tool_exceptions
    def search_text_messages(
        ctx: Context,
        contact_name: str,
        query: Optional[str] = None,
        start_timestamp: Optional[int] = None,
        end_timestamp: Optional[int] = None,
        limit: Optional[int] = 100,
    ) -> str:
        """
        Search text messages in one contact/chatroom with optional keyword and timestamp range.
        """
        db = _database_service()
        if not db or not getattr(db, "is_available", False):
            return _json(_database_unavailable_payload(db))
        contact = _resolve_contact_name(contact_name)
        if not contact:
            return _json(
                {
                    "status": "not_found",
                    "message": f"未找到联系人或群聊: {contact_name}",
                }
            )
        bounded_limit = _bounded_int(limit, 100, 1, 500)
        rows = db.query_text_messages(
            username=contact.username,
            query=query,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            limit=bounded_limit,
        )
        rows.reverse()
        messages = []
        for row in rows:
            content, sender_username, db_path, create_time, server_id, ct_flag = _unpack_text_search_row(row)
            sender = contact_in_chat(db, db.get_contact_by_username(sender_username), contact.username)
            messages.append(
                {
                    "text": _message_content_text(content, ct_flag, 4000, chat_username=contact.username, sender_username=sender_username),
                    "sender_username": sender_username or "",
                    "sender_display": getattr(sender, "display_name", "") if sender else sender_username or "",
                    "create_time": create_time,
                    "time": _format_timestamp(create_time),
                    "server_id": str(server_id or ""),
                    "db_path": str(db_path or ""),
                }
            )
        return _json(
            {
                "status": "ok",
                "contact": _contact_payload(contact),
                "query": query or "",
                "count": len(messages),
                "messages": messages,
            }
        )

    @mcp.tool(structured_output=False)
    @handle_tool_exceptions
    def get_recent_media_messages(
        ctx: Context,
        contact_name: str,
        limit: Optional[int] = 20,
    ) -> str | list[TextContent | ImageContent]:
        """
        Get recent non-text messages with parsed media/file paths and decoded pictures.
        Returns JSON metadata first, followed by native MCP image blocks for vision models.
        image_content_index and quote_image_content_index map images to their content blocks.
        """
        db = _database_service()
        if not db or not getattr(db, "is_available", False):
            return _json(_database_unavailable_payload(db))
        contact = _resolve_contact_name(contact_name)
        if not contact:
            return _json(
                {
                    "status": "not_found",
                    "message": f"未找到联系人或群聊: {contact_name}",
                }
            )
        bounded_limit = _bounded_int(limit, 20, 1, 100)
        rows = _rows_for_contact(db, contact, max(bounded_limit * 3, bounded_limit))
        messages = []
        for row in rows:
            local_type = row[2] if len(row) > 2 else None
            if local_type in (MessageType.Text, MessageType.Text2):
                continue
            messages.append(_factory_message_payload(db, contact, row))
            if len(messages) >= bounded_limit:
                break
        return _media_tool_result(
            {
                "status": "ok",
                "contact": _contact_payload(contact),
                "count": len(messages),
                "messages": messages,
            }
        )

    @mcp.tool()
    @handle_tool_exceptions
    def get_chat_summary_context(
        ctx: Context,
        contact_name: str,
        limit: Optional[int] = 40,
    ) -> str:
        """
        Return compact chronological chat lines suitable for LLM context windows.
        """
        db = _database_service()
        if not db or not getattr(db, "is_available", False):
            return _json(_database_unavailable_payload(db))
        contact = _resolve_contact_name(contact_name)
        if not contact:
            return _json(
                {
                    "status": "not_found",
                    "message": f"未找到联系人或群聊: {contact_name}",
                }
            )
        bounded_limit = max(1, min(int(limit or 40), 120))
        rows = db.get_messages_by_username(contact.username, count=bounded_limit)
        rows.reverse()
        messages = [_message_payload(db, row, include_text=True, chat_username=contact.username if contact else "") for row in rows]
        return _json(
            {
                "status": "ok",
                "contact": _contact_payload(contact),
                "count": len(messages),
                "context": "\n".join(_chat_line(message) for message in messages),
            }
        )

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
        Supports WeChat ID, nickname/remark fuzzy match, text keyword, and time range.
        """
        db = getattr(bot, "database_service", None) if bot is not None else None
        if not db or not getattr(db, "is_available", False):
            return json.dumps(
                {
                    "status": "unavailable",
                    "message": "Linux WeChat database service is not available.",
                    "detail": getattr(db, "last_error", "") if db else "db service missing",
                },
                ensure_ascii=False,
            )

        contact = db.get_contact_by_username(contact_name)
        if not contact:
            matches = db.get_contact_by_display_name(contact_name)
            contact = matches[0] if matches else None
        if not contact:
            return json.dumps(
                {
                    "status": "not_found",
                    "message": f"未找到联系人或群聊: {contact_name}",
                },
                ensure_ascii=False,
            )

        msg_list = db.query_text_messages(
            username=contact.username,
            query=query,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            limit=limit or 500,
        )
        msg_list.reverse()
        processed = []
        for row in msg_list:
            content, sender_username, _db_path, create_time, server_id, ct_flag = _unpack_text_search_row(row)
            sender = contact_in_chat(db, db.get_contact_by_username(sender_username), contact.username)
            processed.append(
                {
                    "from": sender.display_name if sender else sender_username or "未知发件人",
                    "text": _message_content_text(content, ct_flag, 4000, chat_username=contact.username, sender_username=sender_username),
                    "create_time": create_time,
                    "server_id": str(server_id),
                }
            )
        logger.info("为 '%s' 查询到 %s 条文本消息", contact.display_name, len(processed))
        return json.dumps(processed, ensure_ascii=False, indent=None)

    @mcp.tool()
    @handle_tool_exceptions
    def get_rpa_result(ctx: Context, action_id: str) -> str:
        """Read the original RPA task result after queued/timeout; never resubmit it.

        executed means text/image/file/voice sends passed database verification.
        failed or unknown must not trigger an automatic resend: check recent
        messages first. Results are in memory (the latest 1000 tasks).
        """
        service = getattr(bot, "rpa_service", None)
        if service is None:
            return _unavailable_tool("get_rpa_result", "Local RPA result service is unavailable")
        return _json(service.get_result(str(action_id)))

    @mcp.tool()
    @handle_tool_exceptions
    def send_text_msg(
        ctx: Context,
        recipient_name: str,
        message: str,
        at_user_name: Optional[str] = None,
        dry_run: bool = False,
        wait_seconds: Optional[float] = 12,
    ) -> str:
        """
        Send a text message to a user or group via RPA.
        Supports @mention in groups. Set dry_run=true to resolve the target without sending.
        wait_seconds controls whether the local RPA execution result is awaited.
        Only executed confirms a database-verified send. For queued/timeout,
        query get_rpa_result(action_id). On failed, inspect recent messages;
        never automatically resend an unconfirmed message.
        """
        message = str(message or "")
        if not message:
            return _json({"status": "invalid_request", "message": "message is required"})

        resolved = _resolve_rpa_target(recipient_name)
        if "error" in resolved:
            return _rpa_target_not_found(resolved)
        username = resolved["username"]
        nickname = resolved["target"]
        is_chatroom = bool(resolved["is_chatroom"])

        payload = {
            "local_type": MessageType.Text,
            "message_content": message,
            "username": username,
            "nickname": nickname,
            "at_list": [at_user_name] if at_user_name else [],
            "is_chatroom": is_chatroom,
            "create_time": int(time.time()),
        }
        response = {
            "status": "dry_run" if dry_run else "queued",
            "recipient_name": recipient_name,
            "resolved": resolved,
            "message_preview": message[:200],
            "at_user_name": at_user_name,
        }
        if dry_run:
            return _json(response)
        if not _mcp_write_enabled():
            return _mcp_write_blocked("send_text_msg")
        app_context = _get_app_context_from_request(ctx)
        topic = f"msg/{app_context.userinfo.account}/rpa_action"
        wait_timeout = _bounded_float(wait_seconds, 12, 0, 30)
        dispatch_wait = getattr(app_context.command_dispatcher, "dispatch_wait", None)
        if callable(dispatch_wait):
            dispatch_result = dispatch_wait(topic, payload, timeout=wait_timeout)
        else:
            app_context.command_dispatcher.dispatch(topic, payload)
            dispatch_result = {
                "status": "queued",
                "queued": True,
                "completed": False,
                "dispatcher": type(app_context.command_dispatcher).__name__,
            }
        response.update(dispatch_result)
        response["dispatcher"] = type(app_context.command_dispatcher).__name__
        response["wait_seconds"] = wait_timeout
        return _json(response)

    @mcp.tool()
    @handle_tool_exceptions
    def send_file_msg(
        ctx: Context,
        recipient_name: str,
        file_path: str,
        dry_run: bool = False,
        wait_seconds: Optional[float] = 12,
    ) -> str:
        """
        Send a local file to a user or group via RPA.
        file_path must be readable inside the container.
        """
        if not str(recipient_name or "").strip():
            return _json({"status": "invalid_request", "message": "recipient_name is required"})
        path = Path(str(file_path or "")).expanduser()
        if not path.is_file():
            return _json(
                {
                    "status": "invalid_request",
                    "message": "file_path must point to an existing file inside the container",
                    "file_path": str(file_path or ""),
                }
            )
        resolved = _resolve_rpa_target(recipient_name)
        if "error" in resolved:
            return _rpa_target_not_found(resolved)
        action_data = {
            "target": resolved["target"],
            "target_username": resolved["username"],
            "file_path": str(path.resolve()),
            "is_chatroom": bool(resolved["is_chatroom"]),
        }
        return _dispatch_rpa_tool(
            ctx,
            tool_name="send_file_msg",
            action_type=RPAActionType.SEND_FILE.value,
            action_data=action_data,
            dry_run=dry_run,
            wait_seconds=wait_seconds,
            extra={
                "recipient_name": recipient_name,
                "resolved": resolved,
                "file_path": str(path.resolve()),
            },
        )

    @mcp.tool()
    @handle_tool_exceptions
    def send_voice_msg(
        ctx: Context,
        recipient_name: str,
        file_path: Optional[str] = None,
        audio_base64: Optional[str] = None,
        dry_run: bool = False,
        wait_seconds: Optional[float] = 12,
    ) -> str:
        """Send a native voice bubble using WeChat's microphone recording UI.

        Supply exactly one local file_path or audio_base64 (audio data URI also
        accepted). FFmpeg decodes WAV/MP3/M4A/OGG/FLAC etc.; max 10 MiB, 1–55 s.
        Requires Linux WeChat 4.1.13 light theme, 100% DPI, PulseAudio and the
        local RPA queue. dry_run decodes to temporary files then removes them;
        it does not open the microphone or send. Playback takes real time.
        queued/timeout: query get_rpa_result(action_id), never automatically
        resend. Verification checks new acknowledged voice metadata/duration,
        not the spoken content or the recipient's playback.
        """
        if not str(recipient_name or "").strip():
            return _json({"status": "invalid_request", "message": "recipient_name is required"})
        if not dry_run and not _mcp_write_enabled():
            return _mcp_write_blocked("send_voice_msg")
        resolved = _resolve_rpa_target(recipient_name)
        if "error" in resolved:
            return _rpa_target_not_found(resolved)
        directory = None
        transferred = False

        def mark_queued():
            nonlocal transferred
            transferred = True

        try:
            directory = tempfile.TemporaryDirectory(prefix="wechat_ai_voice_")
            path, metadata = prepare_voice(directory.name, file_path=file_path, audio_base64=audio_base64)
            action_data = {
                "target": resolved["target"], "target_username": resolved["username"],
                "is_chatroom": bool(resolved["is_chatroom"]),
            }
            if not dry_run:
                action_data["file_path"] = str(path)
            return _dispatch_rpa_tool(
                ctx, tool_name="send_voice_msg", action_type=RPAActionType.SEND_VOICE.value,
                action_data=action_data, dry_run=dry_run, wait_seconds=wait_seconds,
                extra={"recipient_name": recipient_name, "resolved": resolved, "audio": metadata},
                on_complete=directory.cleanup, on_queued=mark_queued,
            )
        except (ValueError, OSError) as exc:
            return _json({"status": "invalid_request", "message": str(exc)})
        except RuntimeError as exc:
            return _json({"status": "unavailable", "message": str(exc)})
        finally:
            if directory is not None and not transferred:
                directory.cleanup()

    @mcp.tool()
    @handle_tool_exceptions
    def send_image_msg(
        ctx: Context,
        recipient_name: str,
        image_base64: str,
        mime_type: Optional[str] = "image/png",
        file_name: Optional[str] = None,
        dry_run: bool = False,
        wait_seconds: Optional[float] = 12,
    ) -> str:
        """Send uploaded image bytes to a contact/group via the existing file RPA action.

        Accepts strict base64 or an image data URI, up to 10 MiB decoded.
        PNG/JPEG/WebP/GIF signatures override MIME and filename extension hints.
        dry_run validates without writing files or queueing. A timeout does not
        cancel the send: the worker retains the file until execution finishes.
        Query get_rpa_result(action_id) after queued/timeout. A failed result
        must not trigger an automatic resend; check recent messages first.
        """
        if not str(recipient_name or "").strip():
            return _json({"status": "invalid_request", "message": "recipient_name is required"})
        if not dry_run and not _mcp_write_enabled():
            return _mcp_write_blocked("send_image_msg")
        try:
            data, image = decode_image_upload(image_base64, file_name)
        except ValueError as exc:
            return _json({"status": "invalid_request", "message": str(exc)})
        resolved = _resolve_rpa_target(recipient_name)
        if "error" in resolved:
            return _rpa_target_not_found(resolved)
        extra = {
            "recipient_name": recipient_name,
            "resolved": resolved,
            "image": image,
            "dry_run": dry_run,
            "queued": False,
            "completed": False,
            "result": None,
            "wait_seconds": _bounded_float(wait_seconds, 12, 0, 60),
        }
        action_data = {
            "target": resolved["target"], "target_username": resolved["username"],
            "is_chatroom": bool(resolved["is_chatroom"]),
        }
        if dry_run:
            return _dispatch_rpa_tool(
                ctx, tool_name="send_image_msg",
                action_type=RPAActionType.SEND_FILE.value, action_data=action_data,
                dry_run=True, wait_seconds=wait_seconds, extra=extra,
            )
        directory = None
        transferred = False

        def mark_queued() -> None:
            nonlocal transferred
            transferred = True

        try:
            directory = tempfile.TemporaryDirectory(prefix="wechat_ai_send_images_")
            path = Path(directory.name) / (uuid.uuid4().hex + Path(image["file_name"]).suffix)
            # The desktop WeChat process runs as a different user from the bot.
            # It must be able to traverse the directory and read the pasted file.
            Path(directory.name).chmod(0o755)
            with path.open("xb") as stream:
                stream.write(data)
            path.chmod(0o644)
            action_data["file_path"] = str(path)
            return _dispatch_rpa_tool(
                ctx, tool_name="send_image_msg",
                action_type=RPAActionType.SEND_FILE.value, action_data=action_data,
                dry_run=False, wait_seconds=wait_seconds, extra=extra,
                on_complete=directory.cleanup, on_queued=mark_queued,
            )
        except Exception:
            logger.exception("Image upload dispatch failed")
            return _json({
                **extra, "status": "error", "queued": transferred,
                "message": "Unable to stage or dispatch the uploaded image",
            })
        finally:
            # Once enqueued the worker owns cleanup, including timeout/zero-wait
            # responses. Removing the file here would race the RPA file reader.
            if directory is not None and not transferred:
                directory.cleanup()

    @mcp.tool()
    @handle_tool_exceptions
    def send_pat_msg(
        ctx: Context,
        user_name: str,
        room_name: Optional[str] = None,
        dry_run: bool = False,
        wait_seconds: Optional[float] = 12,
    ) -> str:
        """Send a WeChat pat (拍一拍) in a direct chat or a group chat."""
        user_name = str(user_name or "").strip()
        room_name = str(room_name or "").strip() if room_name else None
        if not user_name:
            return _json({"status": "invalid_request", "message": "user_name is required"})
        resolved = _resolve_rpa_target(room_name or user_name)
        if "error" in resolved:
            return _rpa_target_not_found(resolved)
        action_data = {
            "target": resolved["target"],
            "user_name": user_name,
            "is_chatroom": bool(room_name),
        }
        return _dispatch_rpa_tool(
            ctx,
            tool_name="send_pat_msg",
            action_type=RPAActionType.PAT.value,
            action_data=action_data,
            dry_run=dry_run,
            wait_seconds=wait_seconds,
            extra={
                "room_name": room_name,
                "user_name": user_name,
                "resolved": resolved,
            },
        )

    @mcp.tool()
    @handle_tool_exceptions
    def leave_room(ctx: Context, room_name: str, confirm: bool = False) -> str:
        """Leave a group chat."""
        if not confirm:
            return _mcp_confirmation_required("leave_room", "Leaving a group requires explicit confirm=true.")
        if not _mcp_write_enabled():
            return _mcp_write_blocked("leave_room")
        app_context = _get_app_context_from_request(ctx)
        return app_context.command_dispatcher.dispatch_rpa(
            RPAActionType.LEAVE_ROOM.value, {"target": room_name}
        )

    @mcp.tool()
    @handle_tool_exceptions
    def public_room_announcement(
        ctx: Context, room_name: str, content: str, force_edit: bool = False, confirm: bool = False
    ) -> str:
        """Publish or edit a group announcement."""
        if not confirm:
            return _mcp_confirmation_required(
                "public_room_announcement",
                "Publishing a group announcement requires explicit confirm=true.",
            )
        if not _mcp_write_enabled():
            return _mcp_write_blocked("public_room_announcement")
        app_context = _get_app_context_from_request(ctx)
        return app_context.command_dispatcher.dispatch_rpa(
            RPAActionType.PUBLIC_ROOM_ANNOUNCEMENT.value,
            {"target": room_name, "content": content, "force_edit": force_edit},
        )

    @mcp.tool()
    @handle_tool_exceptions
    def query_room_member_list(ctx: Context, room_name: str) -> str:
        """Query group member list from the Linux WeChat contact DB."""
        db = _database_service()
        if not db or not getattr(db, "is_available", False):
            return _json(_database_unavailable_payload(db))
        room = _resolve_contact_name(room_name)
        if not room or not room.username.endswith("@chatroom"):
            return _json({"status": "not_found", "message": f"未找到群聊: {room_name}"})
        members = db.get_room_member_list(room.username)
        return _json(
            {
                "status": "ok",
                "room": _contact_payload(room),
                "count": len(members),
                "members": [_contact_payload(member) for member in members],
            }
        )

    @mcp.tool()
    @handle_tool_exceptions
    def remove_room_member(
        ctx: Context,
        room_name: str,
        member_name: str,
        dry_run: bool = False,
        wait_seconds: Optional[float] = 12,
        confirm: bool = False,
    ) -> str:
        """Remove a member from a group chat via RPA."""
        room_name = str(room_name or "").strip()
        member_name = str(member_name or "").strip()
        if not room_name or not member_name:
            return _json({"status": "invalid_request", "message": "room_name and member_name are required"})
        if not dry_run and not confirm:
            return _mcp_confirmation_required(
                "remove_room_member",
                "Removing a group member requires explicit confirm=true.",
            )
        resolved = _resolve_rpa_target(room_name)
        if "error" in resolved:
            return _rpa_target_not_found(resolved)
        action_data = {"target": resolved["target"], "user_name": member_name}
        return _dispatch_rpa_tool(
            ctx,
            tool_name="remove_room_member",
            action_type=RPAActionType.REMOVE_ROOM_MEMBER.value,
            action_data=action_data,
            dry_run=dry_run,
            wait_seconds=wait_seconds,
            extra={
                "room_name": room_name,
                "member_name": member_name,
                "resolved": resolved,
            },
        )

    @mcp.tool()
    @handle_tool_exceptions
    def invite_room_member(
        ctx: Context,
        room_name: str,
        user_name: str,
        dry_run: bool = False,
        wait_seconds: Optional[float] = 12,
        confirm: bool = False,
    ) -> str:
        """Invite a contact into a group chat via RPA."""
        room_name = str(room_name or "").strip()
        user_name = str(user_name or "").strip()
        if not room_name or not user_name:
            return _json({"status": "invalid_request", "message": "room_name and user_name are required"})
        if not dry_run and not confirm:
            return _mcp_confirmation_required(
                "invite_room_member",
                "Inviting a group member requires explicit confirm=true.",
            )
        resolved = _resolve_rpa_target(room_name)
        if "error" in resolved:
            return _rpa_target_not_found(resolved)
        action_data = {"target": resolved["target"], "user_name": user_name}
        return _dispatch_rpa_tool(
            ctx,
            tool_name="invite_room_member",
            action_type=RPAActionType.INVITE_2_ROOM.value,
            action_data=action_data,
            dry_run=dry_run,
            wait_seconds=wait_seconds,
            extra={
                "room_name": room_name,
                "user_name": user_name,
                "resolved": resolved,
            },
        )

    @mcp.tool()
    @handle_tool_exceptions
    def rename_room_name(
        ctx: Context,
        room_name: str,
        new_name: str,
        dry_run: bool = False,
        wait_seconds: Optional[float] = 25,
        confirm: bool = False,
    ) -> str:
        """Rename a group chat via RPA."""
        room_name = str(room_name or "").strip()
        new_name = str(new_name or "").strip()
        if not room_name or not new_name:
            return _json({"status": "invalid_request", "message": "room_name and new_name are required"})
        if not dry_run and not confirm:
            return _mcp_confirmation_required(
                "rename_room_name",
                "Renaming a group requires explicit confirm=true.",
            )
        resolved = _resolve_rpa_target(room_name)
        if "error" in resolved:
            return _rpa_target_not_found(resolved)
        action_data = {"target": resolved["target"], "name": new_name}
        return _dispatch_rpa_tool(
            ctx,
            tool_name="rename_room_name",
            action_type=RPAActionType.RENAME_ROOM_NAME.value,
            action_data=action_data,
            dry_run=dry_run,
            wait_seconds=wait_seconds,
            extra={
                "room_name": room_name,
                "new_name": new_name,
                "resolved": resolved,
            },
        )

    @mcp.tool()
    @handle_tool_exceptions
    def rename_name_in_room(
        ctx: Context,
        room_name: str,
        new_name_in_room: str,
        dry_run: bool = False,
        wait_seconds: Optional[float] = 25,
        confirm: bool = False,
    ) -> str:
        """Rename the current user's display name in a group chat via RPA."""
        room_name = str(room_name or "").strip()
        new_name_in_room = str(new_name_in_room or "").strip()
        if not room_name or not new_name_in_room:
            return _json({"status": "invalid_request", "message": "room_name and new_name_in_room are required"})
        if not dry_run and not confirm:
            return _mcp_confirmation_required(
                "rename_name_in_room",
                "Renaming your group display name requires explicit confirm=true.",
            )
        resolved = _resolve_rpa_target(room_name)
        if "error" in resolved:
            return _rpa_target_not_found(resolved)
        action_data = {"target": resolved["target"], "name": new_name_in_room}
        return _dispatch_rpa_tool(
            ctx,
            tool_name="rename_name_in_room",
            action_type=RPAActionType.RENAME_NAME_IN_ROOM.value,
            action_data=action_data,
            dry_run=dry_run,
            wait_seconds=wait_seconds,
            extra={
                "room_name": room_name,
                "new_name_in_room": new_name_in_room,
                "resolved": resolved,
            },
        )

    return mcp
