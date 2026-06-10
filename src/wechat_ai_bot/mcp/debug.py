"""Management and debug routes for the bot runtime."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime
from mimetypes import guess_type
from pathlib import Path
from queue import Empty, Queue
from typing import Any
from urllib.parse import quote

from PIL import Image, ImageDraw

from wechat_ai_bot.common.queues import get_queue_stats
from wechat_ai_bot.services.core.linux_database_discovery import LinuxDatabaseDiscovery
from wechat_ai_bot.services.core.wechat_dat import (
    WeChatDatError,
    decrypt_dat_file,
    infer_xor_key,
    parse_dat_file,
)


_DASHBOARD_USERNAME_ENV = "WECHAT_AI_DASHBOARD_USERNAME"
_DASHBOARD_PASSWORD_ENV = "WECHAT_AI_DASHBOARD_PASSWORD"
_DEFAULT_DASHBOARD_USERNAME = "wechat"
_DEFAULT_DASHBOARD_PASSWORD = "wechat"
_DASHBOARD_AUTH_REALM = "WeChat-AI Dashboard"
_DASHBOARD_MUTATION_HEADER = "x-wechat-ai-dashboard"


def register_debug_routes(mcp: Any, bot: Any, config: Any) -> None:
    """Register manager routes on a FastMCP instance."""
    from starlette.requests import Request
    from starlette.responses import Response

    @mcp.custom_route("/dashboard", methods=["GET"], include_in_schema=False)
    async def dashboard_page(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        return Response(_dashboard_html(), media_type="text/html; charset=utf-8")

    @mcp.custom_route("/dashboard/", methods=["GET"], include_in_schema=False)
    async def dashboard_page_slash(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        return Response(_dashboard_html(), media_type="text/html; charset=utf-8")

    _register_dashboard_api_routes(mcp, bot, config, "/dashboard")


def _register_dashboard_api_routes(mcp: Any, bot: Any, config: Any, prefix: str) -> None:
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response

    @mcp.custom_route(f"{prefix}/api/status", methods=["GET"], include_in_schema=False)
    async def debug_status(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        show_sensitive = _parse_bool(
            request.query_params.get("show_sensitive"),
            default=_parse_bool(_config_get(config, "debug.show_sensitive", False), default=False),
        )
        return JSONResponse(build_debug_status(bot, config, show_sensitive=show_sensitive))

    @mcp.custom_route(f"{prefix}/api/database/rescan", methods=["POST"], include_in_schema=False)
    async def debug_database_rescan(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        mutation_response = _require_dashboard_mutation_header(request)
        if mutation_response is not None:
            return mutation_response
        database_service = getattr(bot, "database_service", None)
        if not database_service or not hasattr(database_service, "refresh"):
            return JSONResponse({"ok": False, "error": "database service unavailable"}, status_code=503)
        try:
            status = database_service.refresh()
            return JSONResponse({"ok": True, "database": status})
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )

    @mcp.custom_route(f"{prefix}/api/message/pause", methods=["POST"], include_in_schema=False)
    async def debug_message_pause(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        mutation_response = _require_dashboard_mutation_header(request)
        if mutation_response is not None:
            return mutation_response
        message_service = getattr(bot, "message_service", None)
        if not message_service or not hasattr(message_service, "pause"):
            return JSONResponse({"ok": False, "error": "message service unavailable"}, status_code=503)
        message_service.pause()
        return JSONResponse({"ok": True, "message": _call_status(message_service)})

    @mcp.custom_route(f"{prefix}/api/message/resume", methods=["POST"], include_in_schema=False)
    async def debug_message_resume(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        mutation_response = _require_dashboard_mutation_header(request)
        if mutation_response is not None:
            return mutation_response
        message_service = getattr(bot, "message_service", None)
        if not message_service or not hasattr(message_service, "resume"):
            return JSONResponse({"ok": False, "error": "message service unavailable"}, status_code=503)
        message_service.resume()
        return JSONResponse({"ok": True, "message": _call_status(message_service)})

    @mcp.custom_route(f"{prefix}/api/window/reset", methods=["POST"], include_in_schema=False)
    async def debug_window_reset(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        mutation_response = _require_dashboard_mutation_header(request)
        if mutation_response is not None:
            return mutation_response
        window_manager = getattr(bot, "window_manager", None)
        if not window_manager:
            return JSONResponse({"ok": False, "error": "window manager unavailable"}, status_code=503)
        ensure_ready = getattr(window_manager, "ensure_action_ready", None)
        if not callable(ensure_ready):
            return JSONResponse({"ok": False, "error": "window reset is unavailable"}, status_code=503)
        ok = bool(ensure_ready())
        if ok and hasattr(bot, "chat_window_ready"):
            bot.chat_window_ready = True
        return JSONResponse(
            {
                "ok": ok,
                "error": "" if ok else "window reset failed",
                "window": _window_status(window_manager, show_sensitive=True),
            },
            status_code=200 if ok else 500,
        )

    @mcp.custom_route(f"{prefix}/api/contacts", methods=["GET"], include_in_schema=False)
    async def debug_contacts(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        database_service = getattr(bot, "database_service", None)
        if not database_service or not getattr(database_service, "is_available", False):
            return JSONResponse({"ok": False, "error": "database service unavailable"}, status_code=503)
        query = str(request.query_params.get("q", "")).strip()
        try:
            limit = int(request.query_params.get("limit", "30"))
        except ValueError:
            limit = 30
        contact_type = str(request.query_params.get("type", "any") or "any").strip().lower()
        limit = max(1, min(limit, 500))
        contacts = _search_contacts(database_service, query, limit=limit, contact_type=contact_type)
        return JSONResponse(
            {
                "ok": True,
                "query": query,
                "count": len(contacts),
                "contacts": [_contact_payload(contact) for contact in contacts],
            }
        )

    @mcp.custom_route(f"{prefix}/api/chats", methods=["GET"], include_in_schema=False)
    async def debug_chats(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        database_service = getattr(bot, "database_service", None)
        if not database_service or not getattr(database_service, "is_available", False):
            return JSONResponse({"ok": False, "error": "database service unavailable"}, status_code=503)
        try:
            limit = int(request.query_params.get("limit", "80"))
        except ValueError:
            limit = 80
        try:
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            offset = 0
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        chats = _recent_chat_payloads(database_service, limit=limit, offset=offset)
        return JSONResponse(
            {
                "ok": True,
                "count": len(chats),
                "limit": limit,
                "offset": offset,
                "chats": chats,
            }
        )

    @mcp.custom_route(f"{prefix}/api/contact/detail", methods=["GET"], include_in_schema=False)
    async def debug_contact_detail(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        database_service = getattr(bot, "database_service", None)
        if not database_service or not getattr(database_service, "is_available", False):
            return JSONResponse({"ok": False, "error": "database service unavailable"}, status_code=503)
        username = str(request.query_params.get("username", "")).strip()
        contact_name = str(request.query_params.get("contact", "")).strip()
        contact = _resolve_contact(database_service, username or contact_name)
        if not contact:
            return JSONResponse({"ok": False, "error": "contact not found"}, status_code=404)
        members = []
        if bool(getattr(contact, "is_chatroom", False)):
            try:
                members = [
                    _contact_payload(member)
                    for member in database_service.get_room_member_list(contact.username)
                ]
            except Exception:
                members = []
        return JSONResponse(
            {
                "ok": True,
                "contact": _contact_payload(contact),
                "members": members,
                "member_count": len(members),
                "has_message_table": bool(
                    contact.username in (getattr(database_service, "_message_username_map", {}) or {})
                ),
            }
        )

    @mcp.custom_route(f"{prefix}/api/messages", methods=["GET"], include_in_schema=False)
    async def debug_messages(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        database_service = getattr(bot, "database_service", None)
        if not database_service or not getattr(database_service, "is_available", False):
            return JSONResponse({"ok": False, "error": "database service unavailable"}, status_code=503)
        username = str(request.query_params.get("username", "")).strip()
        contact_name = str(request.query_params.get("contact", "")).strip()
        contact = _resolve_contact(database_service, username or contact_name)
        if not contact:
            return JSONResponse({"ok": False, "error": "contact not found"}, status_code=404)
        try:
            limit = int(request.query_params.get("limit", "20"))
        except ValueError:
            limit = 20
        limit = max(1, min(limit, 200))
        query = str(request.query_params.get("query", "")).strip() or None
        try:
            messages = database_service.query_text_messages(
                username=contact.username,
                query=query,
                limit=limit,
            )
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )
        return JSONResponse(
            {
                "ok": True,
                "contact": _contact_payload(contact),
                "count": len(messages),
                "messages": [
                    _text_message_payload(database_service, row)
                    for row in messages
                ],
            }
        )

    @mcp.custom_route(f"{prefix}/api/messages/recent", methods=["GET"], include_in_schema=False)
    async def debug_recent_messages(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        database_service = getattr(bot, "database_service", None)
        if not database_service or not getattr(database_service, "is_available", False):
            return JSONResponse({"ok": False, "error": "database service unavailable"}, status_code=503)
        username = str(request.query_params.get("username", "")).strip()
        contact_name = str(request.query_params.get("contact", "")).strip()
        contact = _resolve_contact(database_service, username or contact_name)
        if not contact:
            return JSONResponse({"ok": False, "error": "contact not found"}, status_code=404)
        try:
            limit = int(request.query_params.get("limit", "80"))
        except ValueError:
            limit = 80
        limit = max(1, min(limit, 300))
        parse_media = str(request.query_params.get("parse_media", "true")).lower() != "false"
        try:
            rows = database_service.get_messages_by_username(contact.username, count=limit)
            rows.reverse()
            messages = [
                _rich_message_payload(database_service, getattr(bot, "message_factory_service", None), contact, row, parse_media=parse_media)
                for row in rows
            ]
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )
        return JSONResponse(
            {
                "ok": True,
                "contact": _contact_payload(contact),
                "count": len(messages),
                "messages": messages,
            }
        )

    @mcp.custom_route(f"{prefix}/api/messages/media", methods=["GET"], include_in_schema=False)
    async def debug_media_messages(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        database_service = getattr(bot, "database_service", None)
        if not database_service or not getattr(database_service, "is_available", False):
            return JSONResponse({"ok": False, "error": "database service unavailable"}, status_code=503)
        username = str(request.query_params.get("username", "")).strip()
        contact_name = str(request.query_params.get("contact", "")).strip()
        contact = _resolve_contact(database_service, username or contact_name)
        if not contact:
            return JSONResponse({"ok": False, "error": "contact not found"}, status_code=404)
        try:
            limit = int(request.query_params.get("limit", "12"))
        except ValueError:
            limit = 12
        limit = max(1, min(limit, 50))
        factory_service = getattr(bot, "message_factory_service", None)
        try:
            raw_rows = database_service.get_messages_by_username(contact.username, count=limit)
            messages = [
                _factory_message_payload(factory_service, table_name, row)
                for table_name, row in _rows_with_table(database_service, contact.username, raw_rows)
            ]
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )
        return JSONResponse(
            {
                "ok": True,
                "contact": _contact_payload(contact),
                "count": len(messages),
                "messages": messages,
            }
        )

    @mcp.custom_route(f"{prefix}/api/rpa/send_text", methods=["POST"], include_in_schema=False)
    async def debug_rpa_send_text(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        mutation_response = _require_dashboard_mutation_header(request)
        if mutation_response is not None:
            return mutation_response
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid json body"}, status_code=400)
        target = str(payload.get("target") or payload.get("recipient_name") or "").strip()
        content = str(payload.get("content") or payload.get("message") or "")
        at_user_name = str(payload.get("at_user_name") or "").strip() or None
        try:
            wait_seconds = float(payload.get("wait_seconds", 8))
        except (TypeError, ValueError):
            wait_seconds = 8
        wait_seconds = max(0, min(wait_seconds, 30))
        if not target:
            return JSONResponse({"ok": False, "error": "target is required"}, status_code=400)
        if not content:
            return JSONResponse({"ok": False, "error": "content is required"}, status_code=400)

        database_service = getattr(bot, "database_service", None)
        contact = _resolve_contact(database_service, target) if database_service else None
        resolved_target = contact.display_name if contact else target
        try:
            from wechat_ai_bot.rpa.action_handlers import SendTextMessageAction
        except ImportError as exc:
            return JSONResponse(
                {"ok": False, "error": f"SendTextMessageAction unavailable: {exc}"},
                status_code=503,
            )
        queue = getattr(bot, "rpa_task_queue", None)
        if queue is None:
            return JSONResponse({"ok": False, "error": "rpa queue unavailable"}, status_code=503)
        result_queue = Queue(maxsize=1) if wait_seconds > 0 else None
        action = SendTextMessageAction(
            content=content,
            target=resolved_target,
            is_chatroom=bool(contact and contact.is_chatroom),
            at_user_name=at_user_name,
            result_queue=result_queue,
        )
        queue.put(action)
        result = None
        if result_queue is not None:
            try:
                result = result_queue.get(timeout=wait_seconds)
            except Empty:
                result = None
        if result is not None:
            return JSONResponse(
                {
                    "ok": bool(result.get("ok")),
                    "error": "" if result.get("ok") else "RPA action failed",
                    "queued": True,
                    "completed": True,
                    "target": resolved_target,
                    "contact": _contact_payload(contact) if contact else None,
                    "queue_size": queue.qsize(),
                    "result": result,
                },
                status_code=200 if result.get("ok") else 500,
            )
        return JSONResponse(
            {
                "ok": True,
                "queued": True,
                "completed": False,
                "target": resolved_target,
                "contact": _contact_payload(contact) if contact else None,
                "queue_size": queue.qsize(),
                "wait_seconds": wait_seconds,
            }
        )

    @mcp.custom_route(f"{prefix}/api/rpa/action", methods=["POST"], include_in_schema=False)
    async def debug_rpa_action(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        mutation_response = _require_dashboard_mutation_header(request)
        if mutation_response is not None:
            return mutation_response
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid json body"}, status_code=400)
        action_type = str(payload.get("action_type") or "").strip()
        action_data = payload.get("action_data") or {}
        if not action_type:
            return JSONResponse({"ok": False, "error": "action_type is required"}, status_code=400)
        if not isinstance(action_data, dict):
            return JSONResponse({"ok": False, "error": "action_data must be an object"}, status_code=400)
        try:
            wait_seconds = float(payload.get("wait_seconds", 8))
        except (TypeError, ValueError):
            wait_seconds = 8
        wait_seconds = max(0, min(wait_seconds, 30))
        queue = getattr(bot, "rpa_task_queue", None)
        if queue is None:
            return JSONResponse({"ok": False, "error": "rpa queue unavailable"}, status_code=503)
        try:
            from wechat_ai_bot.mcp.dispatchers import QueueCommandDispatcher

            dispatcher = QueueCommandDispatcher(bot, getattr(bot, "user_info", None))
            result = dispatcher.dispatch_rpa_wait(action_type, action_data, timeout=wait_seconds)
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )
        ok = result.get("status") in {"queued", "executed", "timeout"}
        if result.get("status") == "failed":
            ok = False
        return JSONResponse(
            {
                "ok": ok,
                "action_type": action_type,
                "action_data": action_data,
                **result,
            },
            status_code=200 if ok else 500,
        )

    @mcp.custom_route(f"{prefix}/api/logs", methods=["GET"], include_in_schema=False)
    async def debug_logs(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        try:
            lines = int(request.query_params.get("lines", "160"))
        except ValueError:
            lines = 160
        lines = max(20, min(lines, 500))
        log_path = Path(_config_get(config, "logging.path", "/config/logs")) / "bot.log"
        text = tail_file(log_path, lines=lines)
        return JSONResponse(
            {
                "path": str(log_path),
                "lines": lines,
                "text": redact_text(text),
            }
        )

    @mcp.custom_route(f"{prefix}/media", methods=["GET"], include_in_schema=False)
    async def debug_media(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        database_service = getattr(bot, "database_service", None)
        raw_path = str(request.query_params.get("path", "")).strip()
        file_path = _resolve_dashboard_media_path(database_service, raw_path)
        if not file_path:
            return PlainTextResponse("media not found", status_code=404)
        headers = {"Cache-Control": "private, max-age=300"}
        user_info = getattr(bot, "user_info", None)
        dat_response = _dashboard_dat_media_response(
            file_path,
            user_info,
            headers=headers,
            as_download=_parse_bool(request.query_params.get("download"), default=False),
        )
        if dat_response is not None:
            return dat_response
        response = FileResponse(
            file_path,
            media_type=_guess_file_media_type(file_path),
            headers=headers,
        )
        disposition = "attachment" if _parse_bool(request.query_params.get("download"), default=False) else "inline"
        response.headers["Content-Disposition"] = _content_disposition(disposition, file_path.name)
        return response

    @mcp.custom_route(f"{prefix}/layout.png", methods=["GET"], include_in_schema=False)
    async def debug_layout(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        png = build_layout_png(bot)
        return Response(png, media_type="image/png")

    @mcp.custom_route(f"{prefix}/raw-screenshot.png", methods=["GET"], include_in_schema=False)
    async def debug_raw_screenshot(request: Request) -> Response:
        auth_response = _require_dashboard_auth(request)
        if auth_response is not None:
            return auth_response
        if not bool(_config_get(config, "debug.allow_raw_screenshot", False)):
            return PlainTextResponse("raw screenshot is disabled", status_code=403)
        png = build_raw_screenshot_png(bot)
        if not png:
            return PlainTextResponse("screenshot unavailable", status_code=503)
        return Response(png, media_type="image/png")


def _require_dashboard_auth(request: Any) -> Any | None:
    username, password, configured = _dashboard_credentials()
    if not configured:
        return _dashboard_unauthorized(
            "Dashboard credentials are not configured. Set "
            f"{_DASHBOARD_USERNAME_ENV} and {_DASHBOARD_PASSWORD_ENV}; "
            f"the default {_DEFAULT_DASHBOARD_USERNAME}/{_DEFAULT_DASHBOARD_PASSWORD} is disabled."
        )

    header = str(request.headers.get("authorization", "") or "")
    prefix = "basic "
    if not header.lower().startswith(prefix):
        return _dashboard_unauthorized("Dashboard authentication required.")

    token = header[len(prefix):].strip()
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
    except Exception:
        return _dashboard_unauthorized("Invalid dashboard credentials.")
    supplied_username, separator, supplied_password = decoded.partition(":")
    if not separator:
        return _dashboard_unauthorized("Invalid dashboard credentials.")
    if (
        _constant_time_equal(supplied_username, username)
        and _constant_time_equal(supplied_password, password)
    ):
        return None
    return _dashboard_unauthorized("Invalid dashboard credentials.")


def _dashboard_credentials() -> tuple[str, str, bool]:
    username = os.environ.get(_DASHBOARD_USERNAME_ENV, _DEFAULT_DASHBOARD_USERNAME)
    password = os.environ.get(_DASHBOARD_PASSWORD_ENV, _DEFAULT_DASHBOARD_PASSWORD)
    username = str(username or "")
    password = str(password or "")
    configured = bool(username and password)
    configured = configured and not (
        username == _DEFAULT_DASHBOARD_USERNAME
        and password == _DEFAULT_DASHBOARD_PASSWORD
    )
    return username, password, configured


def _dashboard_unauthorized(message: str) -> Any:
    from starlette.responses import PlainTextResponse

    response = PlainTextResponse(message, status_code=401)
    response.headers["WWW-Authenticate"] = f'Basic realm="{_DASHBOARD_AUTH_REALM}", charset="UTF-8"'
    response.headers["Cache-Control"] = "no-store"
    return response


def _require_dashboard_mutation_header(request: Any) -> Any | None:
    from starlette.responses import PlainTextResponse

    value = str(request.headers.get(_DASHBOARD_MUTATION_HEADER, "") or "")
    if value == "1":
        return None
    return PlainTextResponse("dashboard mutation header is required", status_code=403)


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def build_debug_status(
    bot: Any,
    config: Any,
    *,
    show_sensitive: bool = False,
) -> dict[str, Any]:
    """Build a JSON-serializable status snapshot without reading chat content."""
    now = time.time()
    started_at = float(getattr(bot, "started_at", now) or now)
    window_manager = getattr(bot, "window_manager", None)
    image_processor = getattr(bot, "image_processor", None)
    visual_service = getattr(bot, "visual_message_service", None)
    message_service = getattr(bot, "message_service", None)
    processor_service = getattr(bot, "processor_service", None)
    rpa_service = getattr(bot, "rpa_service", None)
    plugin_manager = getattr(bot, "plugin_manager", None)
    mqtt_service = getattr(bot, "mqtt_service", None)
    database_service = getattr(bot, "database_service", None)

    db_report = _database_status(
        _config_get(config, "debug.xwechat_files_root", "/config/xwechat_files"),
        database_service,
        show_sensitive=show_sensitive,
    )

    return {
        "generated_at": int(now),
        "runtime": {
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "uptime_seconds": int(max(0, now - started_at)),
            "started_at": int(started_at),
        },
        "bot": {
            "running": bool(getattr(bot, "is_running", False)),
            "chat_window_ready": bool(getattr(bot, "chat_window_ready", False)),
            "user": _user_status(getattr(bot, "user_info", None), show_sensitive),
        },
        "processes": _process_status(),
        "queues": get_queue_stats(),
        "services": {
            "message": _call_status(message_service),
            "processor": _call_status(processor_service),
            "rpa": _call_status(rpa_service),
            "visual": _visual_status(visual_service, show_sensitive),
            "mqtt": _mqtt_status(mqtt_service),
        },
        "window": _window_status(window_manager, show_sensitive=show_sensitive),
        "yolo": {
            "model_loaded": bool(getattr(image_processor, "yolo", None)),
            "model_path": str(getattr(image_processor, "model_path", "")),
            "imgsz_config": getattr(visual_service, "yolo_imgsz", None),
            "imgsz_actual": getattr(visual_service, "_last_yolo_imgsz", None),
            "stride": getattr(visual_service, "yolo_stride", None),
        },
        "database": _expand_database_status(database_service, db_report, show_sensitive=show_sensitive),
        "media": _media_status(getattr(bot, "user_info", None)),
        "plugins": _plugin_status(plugin_manager),
        "debug": {
            "raw_screenshot_enabled": bool(_config_get(config, "debug.allow_raw_screenshot", False)),
            "show_sensitive": show_sensitive,
        },
    }


def build_layout_png(bot: Any) -> bytes:
    """Return a neutral layout diagram PNG; no WeChat pixels are exposed."""
    window_manager = getattr(bot, "window_manager", None)
    size_config = getattr(window_manager, "size_config", None)
    width = int(getattr(size_config, "width", 1280) or 1280)
    height = int(getattr(size_config, "height", 900) or 900)
    width = max(320, min(width, 2560))
    height = max(240, min(height, 1800))

    img = Image.new("RGB", (width, height), "#f4f6f8")
    draw = ImageDraw.Draw(img)

    sidebar = int(getattr(window_manager, "SIDE_BAR_WIDTH", 0) or 0)
    sessions = int(getattr(window_manager, "SESSION_LIST_WIDTH", 0) or 0)
    msg_x = int(getattr(window_manager, "MSG_TOP_X", 0) or 0)
    msg_y = int(getattr(window_manager, "MSG_TOP_Y", 0) or 0)
    msg_w = int(getattr(window_manager, "MSG_WIDTH", 0) or 0)
    msg_h = int(getattr(window_manager, "MSG_HEIGHT", 0) or 0)
    send = (
        getattr(window_manager, "ICON_CONFIGS", {})
        .get("send_button", {})
        .get("position")
    )

    draw.rectangle([0, 0, width - 1, height - 1], outline="#3b4652", width=2)
    if sidebar > 0:
        draw.rectangle([0, 0, sidebar, height], fill="#d9e2ec", outline="#65758b", width=2)
        draw.text((12, 14), "侧栏", fill="#22303d")
    if sessions > 0:
        draw.rectangle(
            [sidebar, 0, sidebar + sessions, height],
            fill="#e8edf3",
            outline="#65758b",
            width=2,
        )
        draw.text((sidebar + 12, 14), "会话列表", fill="#22303d")
    if msg_w > 0 and msg_h > 0:
        draw.rectangle(
            [msg_x, msg_y, msg_x + msg_w, msg_y + msg_h],
            fill="#ffffff",
            outline="#18715b",
            width=4,
        )
        draw.text((msg_x + 12, msg_y + 12), f"消息区域 {msg_w}x{msg_h}", fill="#105745")
    if send and len(send) == 4:
        draw.rectangle([int(v) for v in send], outline="#bb3e03", width=4)
        draw.text((int(send[0]), max(0, int(send[1]) - 22)), "发送", fill="#8a2c02")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_raw_screenshot_png(bot: Any) -> bytes:
    """Return a raw window screenshot PNG when explicitly enabled."""
    window_manager = getattr(bot, "window_manager", None)
    image_processor = getattr(bot, "image_processor", None)
    if not window_manager or not image_processor:
        return b""
    size_config = getattr(window_manager, "size_config", None)
    width = int(getattr(size_config, "width", 0) or 0)
    height = int(getattr(size_config, "height", 0) or 0)
    if width <= 0 or height <= 0:
        return b""
    screenshot = image_processor.take_screenshot(region=[0, 0, width, height])
    if screenshot is None:
        return b""
    buf = io.BytesIO()
    screenshot.save(buf, format="PNG")
    return buf.getvalue()


def tail_file(path: Path, *, lines: int = 160) -> str:
    """Read the last N lines of a text file."""
    if not path.exists():
        return ""
    with path.open("rb") as file:
        file.seek(0, os.SEEK_END)
        size = file.tell()
        block_size = 8192
        data = bytearray()
        while size > 0 and data.count(b"\n") <= lines:
            read_size = min(block_size, size)
            size -= read_size
            file.seek(size)
            data[:0] = file.read(read_size)
    return b"\n".join(data.splitlines()[-lines:]).decode("utf-8", errors="replace")


def redact_text(text: str) -> str:
    """Redact common secret-like values in logs."""
    patterns = [
        r"(api[_-]?key\s*[:=]\s*)[^\s,]+",
        r"(password\s*[:=]\s*)[^\s,]+",
        r"(secret[_-]?key\s*[:=]\s*)[^\s,]+",
        r"(token\s*[:=]\s*)[^\s,]+",
        r"(dbkey\s*[:=]\s*)[^\s,]+",
    ]
    redacted = text
    for pattern in patterns:
        redacted = re.sub(pattern, r"\1***", redacted, flags=re.IGNORECASE)
    return redacted


def _resolve_contact(database_service: Any, name: str) -> Any:
    if not database_service or not name:
        return None
    try:
        contact = database_service.get_contact_by_username(name)
    except Exception:
        contact = None
    if contact:
        return contact
    try:
        matches = database_service.get_contact_by_display_name(name)
    except Exception:
        matches = []
    return matches[0] if matches else None


def _search_contacts(
    database_service: Any,
    query: str,
    *,
    limit: int,
    contact_type: str = "any",
) -> list[Any]:
    if not database_service:
        return []
    contact_type = contact_type if contact_type in {"any", "chatroom", "contact"} else "any"
    if not query:
        contacts = list((getattr(database_service, "_contact_by_username", {}) or {}).values())
        contacts = [
            contact
            for contact in contacts
            if _contact_type_matches(contact, contact_type)
        ]
        contacts.sort(
            key=lambda contact: (
                0 if getattr(contact, "username", "") in (getattr(database_service, "_message_username_map", {}) or {}) else 1,
                str(getattr(contact, "display_name", "") or getattr(contact, "username", "")).lower(),
            )
        )
        return contacts[:limit]
    results: list[Any] = []
    direct = _resolve_contact(database_service, query)
    if direct and _contact_type_matches(direct, contact_type):
        results.append(direct)
    try:
        results.extend(
            contact
            for contact in database_service.get_contact_by_display_name(query)
            if _contact_type_matches(contact, contact_type)
        )
    except Exception:
        pass

    # Manager-only fallback for broad fuzzy inspection across the in-memory cache.
    needle = query.lower()
    cache = getattr(database_service, "_contact_by_username", {}) or {}
    for contact in cache.values():
        fields = [
            getattr(contact, "username", ""),
            getattr(contact, "display_name", ""),
            getattr(contact, "remark", ""),
            getattr(contact, "nick_name", ""),
            getattr(contact, "alias", ""),
        ]
        if not _contact_type_matches(contact, contact_type):
            continue
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


def _contact_type_matches(contact: Any, contact_type: str) -> bool:
    if contact_type == "chatroom":
        return bool(getattr(contact, "is_chatroom", False))
    if contact_type == "contact":
        return not bool(getattr(contact, "is_chatroom", False))
    return True


def _contact_payload(contact: Any) -> dict[str, Any]:
    if not contact:
        return {}
    username = str(getattr(contact, "username", "") or "")
    small_head_url = str(getattr(contact, "small_head_url", "") or "")
    big_head_url = str(getattr(contact, "big_head_url", "") or "")
    return {
        "id": getattr(contact, "id", None),
        "username": username,
        "display_name": str(getattr(contact, "display_name", "") or username),
        "remark": str(getattr(contact, "remark", "") or ""),
        "nick_name": str(getattr(contact, "nick_name", "") or ""),
        "alias": str(getattr(contact, "alias", "") or ""),
        "is_chatroom": bool(getattr(contact, "is_chatroom", False)),
        "local_type": getattr(contact, "local_type", None),
        "room_remark": str(getattr(contact, "room_remark", "") or ""),
        "description": str(getattr(contact, "description", "") or ""),
        "small_head_url": small_head_url,
        "big_head_url": big_head_url,
        "avatar_url": small_head_url or big_head_url,
        "head_img_md5": str(getattr(contact, "head_img_md5", "") or ""),
        "delete_flag": getattr(contact, "delete_flag", None),
        "verify_flag": getattr(contact, "verify_flag", None),
        "chat_room_notify": getattr(contact, "chat_room_notify", None),
    }


def _text_message_payload(database_service: Any, row: tuple) -> dict[str, Any]:
    content, sender_username, db_path, create_time, server_id = row
    sender = None
    try:
        sender = database_service.get_contact_by_username(sender_username)
    except Exception:
        pass
    return {
        "text": _truncate_text(str(content or ""), 4000),
        "sender_username": sender_username or "",
        "sender_display": getattr(sender, "display_name", "") if sender else sender_username or "",
        "db_path": str(db_path or ""),
        "create_time": create_time,
        "time": _format_timestamp(create_time),
        "server_id": str(server_id or ""),
    }


def _recent_chat_payloads(database_service: Any, *, limit: int, offset: int = 0) -> list[dict[str, Any]]:
    username_map = getattr(database_service, "_message_username_map", {}) or {}
    chats: list[dict[str, Any]] = []
    for username in username_map.keys():
        try:
            rows = database_service.get_messages_by_username(username, count=1)
        except Exception:
            continue
        if not rows:
            continue
        row = rows[0]
        contact = database_service.get_contact_by_username(username)
        payload = {
            "contact": _contact_payload(contact) if contact else {"username": username, "display_name": username},
            "last_message": _rich_message_payload(database_service, None, contact, row, parse_media=False),
        }
        payload["sort_seq"] = row[3] if len(row) > 3 else None
        payload["last_time"] = row[5] if len(row) > 5 else None
        chats.append(payload)
    chats.sort(
        key=lambda item: (
            item.get("sort_seq") or 0,
            item.get("last_time") or 0,
        ),
        reverse=True,
    )
    return chats[offset: offset + limit]


def _rich_message_payload(
    database_service: Any,
    factory_service: Any,
    contact: Any,
    row: tuple,
    *,
    parse_media: bool,
) -> dict[str, Any]:
    local_type = row[2] if len(row) > 2 else None
    sender = None
    sender_id = row[4] if len(row) > 4 else None
    db_path = row[17] if len(row) > 17 else None
    try:
        sender = database_service.get_contact_by_sender_id(sender_id, db_path)
    except Exception:
        sender = None
    sender_payload = _contact_payload(sender) if sender else {}
    upload_status = row[7] if len(row) > 7 else None
    current_account = getattr(getattr(database_service, "user_info", None), "account", "")
    direction = "out" if bool(
        (upload_status is not None and int(upload_status or 0) > 0)
        or (sender and getattr(sender, "username", "") == current_account)
    ) else "in"
    payload: dict[str, Any] = {
        "local_id": row[0] if len(row) > 0 else None,
        "server_id": str(row[1] if len(row) > 1 else ""),
        "type": local_type,
        "type_name": _message_type_name(local_type),
        "sort_seq": row[3] if len(row) > 3 else None,
        "status": row[6] if len(row) > 6 else None,
        "upload_status": upload_status,
        "download_status": row[8] if len(row) > 8 else None,
        "sender": sender_payload,
        "sender_username": sender_payload.get("username", ""),
        "sender_display": sender_payload.get("display_name", ""),
        "sender_avatar_url": sender_payload.get("avatar_url", ""),
        "contact_username": getattr(contact, "username", "") if contact else "",
        "contact_display": getattr(contact, "display_name", "") if contact else "",
        "create_time": row[5] if len(row) > 5 else None,
        "time": _format_timestamp(row[5] if len(row) > 5 else None),
        "direction": direction,
        "db_path": str(db_path or ""),
        "raw_content": _truncate_text(str(row[12] if len(row) > 12 else ""), 1200),
    }
    if local_type in (1, 2):
        payload["text"] = _truncate_text(str(row[12] if len(row) > 12 else ""), 4000)
        return payload
    if not parse_media:
        payload["text"] = f"[{payload['type_name']}]"
        return payload
    table_name = f"Msg_{hashlib.md5(str(getattr(contact, 'username', '') or '').encode()).hexdigest()}"
    media_payload = _factory_message_payload(factory_service, table_name, row)
    payload.update(media_payload)
    if not payload.get("text"):
        payload["text"] = media_payload.get("raw_content") or media_payload.get("factory_error") or f"[{payload['type_name']}]"
    _add_media_urls(payload)
    return payload


def _rows_with_table(database_service: Any, username: str, rows: list[tuple]) -> list[tuple[str, tuple]]:
    table_name = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
    return [(table_name, row) for row in rows]


def _factory_message_payload(factory_service: Any, table_name: str, row: tuple) -> dict[str, Any]:
    local_type = row[2] if len(row) > 2 else None
    payload: dict[str, Any] = {
        "local_id": row[0] if len(row) > 0 else None,
        "server_id": str(row[1] if len(row) > 1 else ""),
        "type": local_type,
        "type_name": _message_type_name(local_type),
        "create_time": row[5] if len(row) > 5 else None,
        "time": _format_timestamp(row[5] if len(row) > 5 else None),
        "db_path": str(row[17] if len(row) > 17 else ""),
        "factory_ok": False,
    }
    if not factory_service:
        payload["factory_error"] = "message factory service unavailable"
        payload["raw_content"] = _truncate_text(str(row[12] if len(row) > 12 else ""), 800)
        return payload
    try:
        message = factory_service.create_message((table_name, row))
    except Exception as exc:
        payload["factory_error"] = f"{type(exc).__name__}: {exc}"
        payload["raw_content"] = _truncate_text(str(row[12] if len(row) > 12 else ""), 800)
        return payload
    if not message:
        payload["factory_error"] = "unsupported message type"
        payload["raw_content"] = _truncate_text(str(row[12] if len(row) > 12 else ""), 800)
        return payload

    payload["factory_ok"] = True
    payload["text"] = _message_text(message)
    sender_payload = _contact_payload(getattr(message, "contact", None))
    room_payload = _contact_payload(getattr(message, "room", None))
    payload["sender"] = sender_payload
    payload["sender_display"] = sender_payload.get("display_name", "")
    payload["sender_avatar_url"] = sender_payload.get("avatar_url", "")
    payload["room"] = room_payload
    payload["room_display"] = room_payload.get("display_name", "")
    message_json: dict[str, Any] = {}
    try:
        raw_json = message.to_json()
        if isinstance(raw_json, dict):
            message_json = {
                key: value
                for key, value in raw_json.items()
                if not isinstance(value, (bytes, bytearray))
            }
    except Exception:
        message_json = {}
    if message_json:
        payload["message"] = message_json
        for key in (
            "url",
            "title",
            "description",
            "desc",
            "cover_url",
            "cover_path",
            "thumb_url",
            "app_logo",
            "app_name",
            "app_id",
            "publisher_nickname",
            "publisher_avatar",
            "nickname",
            "alias",
            "username",
            "small_head_url",
            "big_head_url",
            "province",
            "city",
            "sex",
            "sign",
            "open_im_desc",
            "open_im_desc_icon",
            "x",
            "y",
            "label",
            "poiname",
            "scale",
            "voice_to_text",
            "display_content",
            "invite_type",
            "pay_memo",
            "fee_desc",
            "receiver_username",
            "pay_subtype",
            "media_count",
            "width",
            "height",
        ):
            if key in message_json and message_json[key] not in (None, ""):
                payload[key] = message_json[key]
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
        "thumb_url",
    ):
        if hasattr(message, key):
            value = getattr(message, key)
            if isinstance(value, (bytes, bytearray)):
                continue
            payload[key] = str(value) if isinstance(value, Path) else value
    _add_media_urls(payload)
    return payload


def _add_media_urls(payload: dict[str, Any]) -> None:
    for key in ("path", "thumb_path", "cover_path"):
        value = str(payload.get(key) or "")
        if not value:
            continue
        payload[f"{key}_url"] = _media_url(value)
    if payload.get("path_url"):
        payload["media_url"] = payload["path_url"]
    elif payload.get("cover_path_url"):
        payload["media_url"] = payload["cover_path_url"]
    elif payload.get("thumb_path_url"):
        payload["media_url"] = payload["thumb_path_url"]
    elif payload.get("thumb_url"):
        payload["media_url"] = _media_url(str(payload["thumb_url"]))
    size_value = payload.get("file_size")
    if size_value not in (None, ""):
        payload["file_size_label"] = _format_file_size(size_value)


def _media_url(path_or_url: str) -> str:
    if not path_or_url:
        return ""
    if re.match(r"^https?://", path_or_url, flags=re.IGNORECASE):
        return path_or_url
    return f"/dashboard/media?path={quote(path_or_url)}"


def _format_file_size(value: Any) -> str:
    try:
        size = int(float(value or 0))
    except (TypeError, ValueError):
        return str(value or "")
    units = ("B", "KB", "MB", "GB", "TB")
    number = float(size)
    unit = units[0]
    for unit in units:
        if number < 1024 or unit == units[-1]:
            break
        number /= 1024
    if unit == "B":
        return f"{int(number)} {unit}"
    return f"{number:.2f} {unit}"


def _resolve_dashboard_media_path(database_service: Any, raw_path: str) -> Path | None:
    if not raw_path or "\x00" in raw_path or re.match(r"^https?://", raw_path, flags=re.IGNORECASE):
        return None
    candidate = Path(raw_path)
    allowed_roots = _dashboard_media_roots(database_service)
    candidates: list[Path] = [candidate] if candidate.is_absolute() else []
    for root in allowed_roots:
        if not candidate.is_absolute():
            candidates.append(root / candidate)
    for item in candidates:
        try:
            resolved = item.resolve()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        if any(_is_relative_to(resolved, root) for root in allowed_roots):
            return resolved
    return None


def _dashboard_media_roots(database_service: Any) -> list[Path]:
    roots: list[Path] = []
    primary = getattr(database_service, "_primary_account", None)
    for value in (
        getattr(primary, "account_dir", None),
        getattr(primary, "db_storage_dir", None),
        getattr(database_service, "root", None),
        "/config/xwechat_files",
    ):
        if not value:
            continue
        try:
            path = Path(value).resolve()
        except OSError:
            continue
        if path not in roots:
            roots.append(path)
    return roots


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _guess_file_media_type(path: Path) -> str:
    media_type, _encoding = guess_type(str(path))
    if media_type and media_type != "application/octet-stream":
        return media_type
    try:
        with Image.open(path) as image:
            if image.format and image.format in Image.MIME:
                return Image.MIME[image.format]
    except Exception:
        pass
    suffix = path.suffix.lower()
    if suffix in {".mp4", ".m4v"}:
        return "video/mp4"
    if suffix == ".webm":
        return "video/webm"
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix == ".wav":
        return "audio/wav"
    return media_type or "application/octet-stream"


def _dashboard_dat_media_response(
    file_path: Path,
    user_info: Any,
    *,
    headers: dict[str, str],
    as_download: bool,
) -> Any | None:
    try:
        info = parse_dat_file(file_path)
    except WeChatDatError as exc:
        from starlette.responses import JSONResponse

        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
                "path": str(file_path),
                "file_name": file_path.name,
                "dat": True,
            },
            status_code=415,
            headers={**headers, "Cache-Control": "no-store"},
        )
    if info is None:
        return None

    from starlette.responses import JSONResponse, Response

    dat_key = str(getattr(user_info, "dat_key", "") or "").strip()
    dat_xor_key = getattr(user_info, "dat_xor_key", -1)
    inferred_xor_key = _infer_dat_xor_key(file_path, info)
    try:
        result = decrypt_dat_file(file_path, aes_key=dat_key, xor_key=dat_xor_key)
    except WeChatDatError as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
                "path": str(file_path),
                "file_name": file_path.name,
                "dat": True,
                "dat_info": info.to_dict(),
                "has_dat_key": bool(dat_key),
                "dat_xor_key": dat_xor_key,
                "inferred_dat_xor_key": inferred_xor_key,
            },
            status_code=422,
            headers={**headers, "Cache-Control": "no-store"},
        )

    disposition = "attachment" if as_download else "inline"
    response = Response(
        result.data,
        media_type=result.media_type,
        headers=headers,
    )
    response.headers["Content-Disposition"] = _content_disposition(
        disposition,
        f"{file_path.stem}{result.extension}",
    )
    response.headers["X-WeChat-Dat-Version"] = result.info.version
    return response


def _infer_dat_xor_key(file_path: Path, info: Any) -> int | None:
    try:
        return infer_xor_key(file_path.read_bytes(), info)
    except Exception:
        return None


def _content_disposition(disposition: str, filename: str) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._") or "download"
    utf8_name = quote(filename, safe="")
    return f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}'


def _message_text(message: Any) -> str:
    try:
        text = message.to_text()
    except Exception:
        text = getattr(message, "content", "") or getattr(message, "message_content", "")
    return _truncate_text(str(text or ""), 1200)


def _message_type_name(local_type: Any) -> str:
    try:
        from wechat_ai_bot.weixin.message_classes import MessageType

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


def _database_status(root: str, database_service: Any = None, *, show_sensitive: bool = False) -> dict[str, Any]:
    if database_service and hasattr(database_service, "get_status"):
        try:
            status = database_service.get_status()
            status["discovery"] = _database_discovery_status(root, show_sensitive=show_sensitive)
            return status
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
    return _database_discovery_status(root, show_sensitive=show_sensitive)


def _database_discovery_status(root: str, *, show_sensitive: bool = False) -> dict[str, Any]:
    try:
        report = LinuxDatabaseDiscovery(root).scan()
        accounts = []
        for account in report.accounts:
            accounts.append(
                {
                    "account": account.account_id if show_sensitive else mask_value(account.account_id),
                    "storage_suffix": account.storage_suffix,
                    "encrypted_databases": len(account.encrypted_databases),
                    "plaintext_databases": len(account.plaintext_databases),
                    "login_key_db": bool(account.login_key_db),
                    "login_key_dat": bool(account.login_key_dat),
                    "samples": [
                        str(path.relative_to(account.db_storage_dir))
                        for path in account.encrypted_databases[:5]
                    ],
                }
            )
        return {
            "root": str(report.root),
            "accounts": accounts,
            "account_count": len(accounts),
            "sqlcipher_available": report.sqlcipher_available,
            "sqlcipher_driver": report.sqlcipher_driver,
            "can_attempt_business_database_open": report.can_attempt_business_database_open,
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _expand_database_status(
    database_service: Any,
    report: dict[str, Any],
    *,
    show_sensitive: bool,
) -> dict[str, Any]:
    if not show_sensitive or not database_service or not isinstance(report, dict):
        return report
    expanded = dict(report)
    primary = getattr(database_service, "_primary_account", None)
    if primary:
        account_id = getattr(primary, "account_id", None)
        account_dir = getattr(primary, "account_dir", None)
        db_storage_dir = getattr(primary, "db_storage_dir", None)
        if account_id:
            expanded["primary_account"] = str(account_id)
        if account_dir:
            expanded["account_dir"] = str(account_dir)
        if db_storage_dir:
            expanded["db_storage_dir"] = str(db_storage_dir)
    return expanded


def _process_status() -> dict[str, Any]:
    return {
        "wechat": _pgrep_exact("wechat"),
        "bot": _pgrep("wechat_ai_bot.bot"),
        "display": _command(["xdotool", "getdisplaygeometry"], timeout=1.5),
    }


def _window_status(window_manager: Any, *, show_sensitive: bool = False) -> dict[str, Any]:
    if not window_manager:
        return {}
    refresh = getattr(window_manager, "refresh_window_geometry", None)
    if callable(refresh):
        refresh()
    current_session = str(_safe_call(window_manager, "get_current_session_name") or "")
    return {
        "current_window": bool(getattr(window_manager, "current_window", None)),
        "state": getattr(window_manager, "last_window_state", ""),
        "message_region": _safe_call(window_manager, "get_message_region"),
        "target_size": getattr(window_manager, "target_window_size", None),
        "actual_geometry": getattr(window_manager, "actual_window_geometry", {}),
        "aligned": bool(getattr(window_manager, "window_aligned", False)),
        "window_id": getattr(window_manager, "window_id", None),
        "current_session": current_session if show_sensitive else mask_value(current_session),
        "sidebar_width": getattr(window_manager, "SIDE_BAR_WIDTH", 0),
        "session_list_width": getattr(window_manager, "SESSION_LIST_WIDTH", 0),
        "title_bar_height": getattr(window_manager, "TITLE_BAR_HEIGHT", 0),
        "send_button": (
            getattr(window_manager, "ICON_CONFIGS", {})
            .get("send_button", {})
            .get("position")
        ),
    }


def _visual_status(visual_service: Any, show_sensitive: bool) -> dict[str, Any]:
    if not visual_service:
        return {}
    session_name = str(getattr(visual_service, "_current_session_name", "") or "")
    title_text = str(getattr(visual_service, "_last_title_text", "") or "")
    if not show_sensitive:
        session_name = mask_value(session_name)
        title_text = mask_value(title_text)
    return {
        "running": bool(getattr(visual_service, "is_running", False)),
        "poll_interval": getattr(visual_service, "poll_interval", None),
        "primed": bool(getattr(visual_service, "_primed", False)),
        "current_session": session_name,
        "last_title_text": title_text,
        "seen_hashes": len(getattr(visual_service, "_seen_hashes", {}) or {}),
        "seen_visual_hashes": len(getattr(visual_service, "_seen_visual_hashes", {}) or {}),
        "context_cache": len(getattr(visual_service, "_context_text_cache", {}) or {}),
        "dedup_cache_size": getattr(visual_service, "dedup_cache_size", None),
    }


def _mqtt_status(mqtt_service: Any) -> dict[str, Any]:
    if not mqtt_service:
        return {"enabled": False}
    client = getattr(mqtt_service, "mqtt_client", None)
    paho_client = getattr(client, "client", None)
    return {
        "enabled": True,
        "host": getattr(mqtt_service, "host", ""),
        "port": getattr(mqtt_service, "port", ""),
        "connected": bool(getattr(paho_client, "connected_flag", False)),
    }


def _media_status(user_info: Any) -> dict[str, Any]:
    if not user_info:
        return {"dat_key_configured": False, "dat_xor_key": -1}
    dat_key = str(getattr(user_info, "dat_key", "") or "")
    return {
        "dat_key_configured": bool(dat_key),
        "dat_xor_key": getattr(user_info, "dat_xor_key", -1),
    }


def _plugin_status(plugin_manager: Any) -> list[dict[str, Any]]:
    if not plugin_manager:
        return []
    plugins = []
    for plugin in getattr(plugin_manager, "plugins", []) or []:
        name = getattr(plugin, "get_plugin_name", lambda: plugin.__class__.__name__)()
        plugins.append(
            {
                "name": str(name),
                "class": plugin.__class__.__name__,
                "priority": getattr(plugin, "priority", 0),
            }
        )
    return plugins


def _user_status(user_info: Any, show_sensitive: bool) -> dict[str, Any]:
    if not user_info:
        return {}
    data = {}
    for key in ("account", "nickname", "version", "alias", "phone"):
        value = getattr(user_info, key, "")
        data[key] = value if show_sensitive else mask_value(str(value))
    if not show_sensitive:
        data["version"] = getattr(user_info, "version", "")
    return data


def _call_status(service: Any) -> dict[str, Any]:
    if not service:
        return {}
    if hasattr(service, "get_status"):
        try:
            return service.get_status()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
    return {"running": bool(getattr(service, "is_running", False))}


def _safe_call(obj: Any, method_name: str) -> Any:
    if not obj or not hasattr(obj, method_name):
        return None
    try:
        return getattr(obj, method_name)()
    except Exception:
        return None


def _pgrep(pattern: str) -> dict[str, Any]:
    result = _command(["pgrep", "-f", pattern], timeout=1.5)
    pids = [line.strip() for line in result.get("stdout", "").splitlines() if line.strip()]
    return {"running": bool(pids), "pids": pids[:10]}


def _pgrep_exact(name: str) -> dict[str, Any]:
    result = _command(["pgrep", "-x", name], timeout=1.5)
    pids = [line.strip() for line in result.get("stdout", "").splitlines() if line.strip()]
    return {"running": bool(pids), "pids": pids[:10]}


def _command(command: list[str], *, timeout: float = 2) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":1")},
        )
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode,
        }
    except Exception as exc:
        return {"ok": False, "stderr": f"{type(exc).__name__}: {exc}", "returncode": -1}


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if hasattr(config, "get"):
        try:
            return config.get(key, default)
        except TypeError:
            pass
    if not isinstance(config, dict):
        return default
    current: Any = config
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def mask_value(value: str, *, keep_start: int = 2, keep_end: int = 2) -> str:
    if not value:
        return ""
    if len(value) <= keep_start + keep_end + 2:
        return value[:1] + "***"
    return f"{value[:keep_start]}***{value[-keep_end:]}"


def _dashboard_html() -> str:
    html_path = Path(__file__).with_name("dashboard.html")
    try:
        return html_path.read_text(encoding="utf-8")
    except OSError:
        return DEBUG_HTML


DEBUG_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WeChat-AI Dashboard</title>
</head>
<body>
  <h1>WeChat-AI Dashboard</h1>
  <p>dashboard.html is missing from the package.</p>
  <p><a href="/dashboard/api/status">Runtime status JSON</a></p>
</body>
</html>
"""
