"""Management and debug routes for the bot runtime."""

from __future__ import annotations

import io
import hashlib
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from wechat_ai_bot.common.queues import get_queue_stats
from wechat_ai_bot.services.core.linux_database_discovery import LinuxDatabaseDiscovery


def register_debug_routes(mcp: Any, bot: Any, config: Any) -> None:
    """Register manager routes on a FastMCP instance."""
    from starlette.requests import Request
    from starlette.responses import RedirectResponse, Response

    @mcp.custom_route("/dashboard", methods=["GET"], include_in_schema=False)
    async def dashboard_page(request: Request) -> Response:
        return Response(_dashboard_html(), media_type="text/html; charset=utf-8")

    @mcp.custom_route("/dashboard/", methods=["GET"], include_in_schema=False)
    async def dashboard_page_slash(request: Request) -> Response:
        return Response(_dashboard_html(), media_type="text/html; charset=utf-8")

    # Backward compatibility for older local links.
    @mcp.custom_route("/debug", methods=["GET"], include_in_schema=False)
    async def debug_page(request: Request) -> Response:
        return RedirectResponse(url="/dashboard", status_code=307)

    @mcp.custom_route("/debug/", methods=["GET"], include_in_schema=False)
    async def debug_page_slash(request: Request) -> Response:
        return RedirectResponse(url="/dashboard", status_code=307)

    for prefix in ("/dashboard", "/debug"):
        _register_dashboard_api_routes(mcp, bot, config, prefix)


def _register_dashboard_api_routes(mcp: Any, bot: Any, config: Any, prefix: str) -> None:
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse, Response

    @mcp.custom_route(f"{prefix}/api/status", methods=["GET"], include_in_schema=False)
    async def debug_status(request: Request) -> JSONResponse:
        show_sensitive = bool(_config_get(config, "debug.show_sensitive", False))
        return JSONResponse(build_debug_status(bot, config, show_sensitive=show_sensitive))

    @mcp.custom_route(f"{prefix}/api/database/rescan", methods=["POST"], include_in_schema=False)
    async def debug_database_rescan(request: Request) -> JSONResponse:
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
    async def debug_message_pause(request: Request) -> JSONResponse:
        message_service = getattr(bot, "message_service", None)
        if not message_service or not hasattr(message_service, "pause"):
            return JSONResponse({"ok": False, "error": "message service unavailable"}, status_code=503)
        message_service.pause()
        return JSONResponse({"ok": True, "message": _call_status(message_service)})

    @mcp.custom_route(f"{prefix}/api/message/resume", methods=["POST"], include_in_schema=False)
    async def debug_message_resume(request: Request) -> JSONResponse:
        message_service = getattr(bot, "message_service", None)
        if not message_service or not hasattr(message_service, "resume"):
            return JSONResponse({"ok": False, "error": "message service unavailable"}, status_code=503)
        message_service.resume()
        return JSONResponse({"ok": True, "message": _call_status(message_service)})

    @mcp.custom_route(f"{prefix}/api/contacts", methods=["GET"], include_in_schema=False)
    async def debug_contacts(request: Request) -> JSONResponse:
        database_service = getattr(bot, "database_service", None)
        if not database_service or not getattr(database_service, "is_available", False):
            return JSONResponse({"ok": False, "error": "database service unavailable"}, status_code=503)
        query = str(request.query_params.get("q", "")).strip()
        try:
            limit = int(request.query_params.get("limit", "30"))
        except ValueError:
            limit = 30
        limit = max(1, min(limit, 100))
        contacts = _search_contacts(database_service, query, limit=limit)
        return JSONResponse(
            {
                "ok": True,
                "query": query,
                "count": len(contacts),
                "contacts": [_contact_payload(contact) for contact in contacts],
            }
        )

    @mcp.custom_route(f"{prefix}/api/messages", methods=["GET"], include_in_schema=False)
    async def debug_messages(request: Request) -> JSONResponse:
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

    @mcp.custom_route(f"{prefix}/api/messages/media", methods=["GET"], include_in_schema=False)
    async def debug_media_messages(request: Request) -> JSONResponse:
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
    async def debug_rpa_send_text(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid json body"}, status_code=400)
        target = str(payload.get("target") or payload.get("recipient_name") or "").strip()
        content = str(payload.get("content") or payload.get("message") or "")
        at_user_name = str(payload.get("at_user_name") or "").strip() or None
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
        action = SendTextMessageAction(
            content=content,
            target=resolved_target,
            is_chatroom=bool(contact and contact.is_chatroom),
            at_user_name=at_user_name,
        )
        queue.put(action)
        return JSONResponse(
            {
                "ok": True,
                "queued": True,
                "target": resolved_target,
                "contact": _contact_payload(contact) if contact else None,
                "queue_size": queue.qsize(),
            }
        )

    @mcp.custom_route(f"{prefix}/api/logs", methods=["GET"], include_in_schema=False)
    async def debug_logs(request: Request) -> JSONResponse:
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

    @mcp.custom_route(f"{prefix}/layout.png", methods=["GET"], include_in_schema=False)
    async def debug_layout(request: Request) -> Response:
        png = build_layout_png(bot)
        return Response(png, media_type="image/png")

    @mcp.custom_route(f"{prefix}/raw-screenshot.png", methods=["GET"], include_in_schema=False)
    async def debug_raw_screenshot(request: Request) -> Response:
        if not bool(_config_get(config, "debug.allow_raw_screenshot", False)):
            return PlainTextResponse("raw screenshot is disabled", status_code=403)
        png = build_raw_screenshot_png(bot)
        if not png:
            return PlainTextResponse("screenshot unavailable", status_code=503)
        return Response(png, media_type="image/png")


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
        "window": _window_status(window_manager),
        "yolo": {
            "model_loaded": bool(getattr(image_processor, "yolo", None)),
            "model_path": str(getattr(image_processor, "model_path", "")),
            "imgsz_config": getattr(visual_service, "yolo_imgsz", None),
            "imgsz_actual": getattr(visual_service, "_last_yolo_imgsz", None),
            "stride": getattr(visual_service, "yolo_stride", None),
        },
        "database": db_report,
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
        draw.text((12, 14), "sidebar", fill="#22303d")
    if sessions > 0:
        draw.rectangle(
            [sidebar, 0, sidebar + sessions, height],
            fill="#e8edf3",
            outline="#65758b",
            width=2,
        )
        draw.text((sidebar + 12, 14), "sessions", fill="#22303d")
    if msg_w > 0 and msg_h > 0:
        draw.rectangle(
            [msg_x, msg_y, msg_x + msg_w, msg_y + msg_h],
            fill="#ffffff",
            outline="#18715b",
            width=4,
        )
        draw.text((msg_x + 12, msg_y + 12), f"message {msg_w}x{msg_h}", fill="#105745")
    if send and len(send) == 4:
        draw.rectangle([int(v) for v in send], outline="#bb3e03", width=4)
        draw.text((int(send[0]), max(0, int(send[1]) - 22)), "send", fill="#8a2c02")

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


def _search_contacts(database_service: Any, query: str, *, limit: int) -> list[Any]:
    if not database_service or not query:
        return []
    results: list[Any] = []
    direct = _resolve_contact(database_service, query)
    if direct:
        results.append(direct)
    try:
        results.extend(database_service.get_contact_by_display_name(query))
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
    payload["sender_display"] = getattr(getattr(message, "contact", None), "display_name", "")
    payload["room_display"] = getattr(getattr(message, "room", None), "display_name", "")
    for key in ("path", "thumb_path", "file_name", "file_size", "file_type", "md5", "duration"):
        if hasattr(message, key):
            value = getattr(message, key)
            if isinstance(value, (bytes, bytearray)):
                continue
            payload[key] = str(value) if isinstance(value, Path) else value
    return payload


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


def _database_status(root: str, database_service: Any = None) -> dict[str, Any]:
    if database_service and hasattr(database_service, "get_status"):
        try:
            status = database_service.get_status()
            status["discovery"] = _database_discovery_status(root)
            return status
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
    return _database_discovery_status(root)


def _database_discovery_status(root: str) -> dict[str, Any]:
    try:
        report = LinuxDatabaseDiscovery(root).scan()
        accounts = []
        for account in report.accounts:
            accounts.append(
                {
                    "account": mask_value(account.account_id),
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


def _process_status() -> dict[str, Any]:
    return {
        "wechat": _pgrep("wechat"),
        "bot": _pgrep("wechat_ai_bot.bot"),
        "display": _command(["xdotool", "getdisplaygeometry"], timeout=1.5),
    }


def _window_status(window_manager: Any) -> dict[str, Any]:
    if not window_manager:
        return {}
    return {
        "current_window": bool(getattr(window_manager, "current_window", None)),
        "message_region": _safe_call(window_manager, "get_message_region"),
        "target_size": getattr(window_manager, "target_window_size", None),
        "actual_geometry": getattr(window_manager, "actual_window_geometry", {}),
        "current_session": mask_value(
            str(_safe_call(window_manager, "get_current_session_name") or "")
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
