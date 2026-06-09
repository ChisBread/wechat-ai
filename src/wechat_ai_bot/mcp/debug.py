"""Read-only management and debug routes for the bot runtime."""

from __future__ import annotations

import io
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from wechat_ai_bot.common.queues import get_queue_stats
from wechat_ai_bot.services.core.linux_database_discovery import LinuxDatabaseDiscovery


def register_debug_routes(mcp: Any, bot: Any, config: Any) -> None:
    """Register read-only debug routes on a FastMCP instance."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse, Response

    @mcp.custom_route("/debug", methods=["GET"], include_in_schema=False)
    async def debug_page(request: Request) -> Response:
        return Response(DEBUG_HTML, media_type="text/html; charset=utf-8")

    @mcp.custom_route("/debug/", methods=["GET"], include_in_schema=False)
    async def debug_page_slash(request: Request) -> Response:
        return Response(DEBUG_HTML, media_type="text/html; charset=utf-8")

    @mcp.custom_route("/debug/api/status", methods=["GET"], include_in_schema=False)
    async def debug_status(request: Request) -> JSONResponse:
        show_sensitive = bool(_config_get(config, "debug.show_sensitive", False))
        return JSONResponse(build_debug_status(bot, config, show_sensitive=show_sensitive))

    @mcp.custom_route("/debug/api/logs", methods=["GET"], include_in_schema=False)
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

    @mcp.custom_route("/debug/layout.png", methods=["GET"], include_in_schema=False)
    async def debug_layout(request: Request) -> Response:
        png = build_layout_png(bot)
        return Response(png, media_type="image/png")

    @mcp.custom_route("/debug/raw-screenshot.png", methods=["GET"], include_in_schema=False)
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
    processor_service = getattr(bot, "processor_service", None)
    rpa_service = getattr(bot, "rpa_service", None)
    plugin_manager = getattr(bot, "plugin_manager", None)
    mqtt_service = getattr(bot, "mqtt_service", None)

    db_report = _database_status(_config_get(config, "debug.xwechat_files_root", "/config/xwechat_files"))

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


def _database_status(root: str) -> dict[str, Any]:
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


DEBUG_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WeChat-AI Debug</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7fa;
      --panel: #ffffff;
      --line: #d8e0e8;
      --text: #17212b;
      --muted: #647384;
      --ok: #0f7b5f;
      --warn: #a16207;
      --bad: #b42318;
      --blue: #1d4ed8;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      letter-spacing: 0;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 20px;
      background: #17212b;
      color: #fff;
      border-bottom: 1px solid #0f1720;
    }
    h1 { margin: 0; font-size: 18px; font-weight: 700; }
    main { width: min(1400px, 100%); margin: 0 auto; padding: 16px; }
    .toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    button {
      border: 1px solid #94a3b8;
      background: #fff;
      color: #17212b;
      border-radius: 6px;
      min-height: 34px;
      padding: 0 12px;
      cursor: pointer;
      font: inherit;
    }
    button:hover { border-color: var(--blue); color: var(--blue); }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px; }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      min-width: 0;
    }
    section h2 {
      margin: 0;
      padding: 10px 12px;
      font-size: 14px;
      border-bottom: 1px solid var(--line);
      background: #f9fbfd;
    }
    .span-3 { grid-column: span 3; }
    .span-4 { grid-column: span 4; }
    .span-5 { grid-column: span 5; }
    .span-7 { grid-column: span 7; }
    .span-12 { grid-column: span 12; }
    .body { padding: 12px; }
    .kv { width: 100%; border-collapse: collapse; table-layout: fixed; }
    .kv th, .kv td {
      padding: 7px 8px;
      border-bottom: 1px solid #eef2f6;
      vertical-align: top;
      word-break: break-word;
    }
    .kv th { width: 42%; color: var(--muted); text-align: left; font-weight: 600; }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid var(--line);
      background: #f8fafc;
    }
    .ok { color: var(--ok); border-color: #9bd5c7; background: #eefaf6; }
    .warn { color: var(--warn); border-color: #e9c46a; background: #fff8e1; }
    .bad { color: var(--bad); border-color: #f0a7a0; background: #fff1f0; }
    .muted { color: var(--muted); }
    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .layout-img {
      width: 100%;
      max-height: 560px;
      object-fit: contain;
      background: #f8fafc;
      border: 1px solid var(--line);
      border-radius: 6px;
    }
    .log {
      margin: 0;
      max-height: 420px;
      overflow: auto;
      padding: 12px;
      background: #111827;
      color: #e5e7eb;
      border-radius: 6px;
      line-height: 1.45;
    }
    .list { display: flex; flex-direction: column; gap: 8px; }
    .rowline { display: flex; justify-content: space-between; gap: 12px; border-bottom: 1px solid #eef2f6; padding-bottom: 7px; }
    .rowline:last-child { border-bottom: 0; padding-bottom: 0; }
    @media (max-width: 920px) {
      main { padding: 10px; }
      header { align-items: flex-start; flex-direction: column; }
      .span-3, .span-4, .span-5, .span-7, .span-12 { grid-column: span 12; }
    }
  </style>
</head>
<body>
  <header>
    <h1>WeChat-AI Debug</h1>
    <div class="toolbar">
      <span id="stamp" class="muted">loading</span>
      <button id="refresh">Refresh</button>
      <button id="logs">Logs</button>
    </div>
  </header>
  <main class="grid">
    <section class="span-3"><h2>Runtime</h2><div id="runtime" class="body"></div></section>
    <section class="span-3"><h2>Bot</h2><div id="bot" class="body"></div></section>
    <section class="span-3"><h2>Queues</h2><div id="queues" class="body"></div></section>
    <section class="span-3"><h2>Vision</h2><div id="vision" class="body"></div></section>
    <section class="span-7"><h2>Window Layout</h2><div class="body"><img id="layout" class="layout-img" src="/debug/layout.png" alt="layout"></div></section>
    <section class="span-5"><h2>Database</h2><div id="database" class="body"></div></section>
    <section class="span-4"><h2>Services</h2><div id="services" class="body"></div></section>
    <section class="span-4"><h2>Processes</h2><div id="processes" class="body"></div></section>
    <section class="span-4"><h2>Plugins</h2><div id="plugins" class="body"></div></section>
    <section class="span-12"><h2>Logs</h2><div class="body"><pre id="logText" class="log mono"></pre></div></section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const pill = (ok, text) => `<span class="pill ${ok ? 'ok' : 'bad'}">${esc(text)}</span>`;
    const warn = (text) => `<span class="pill warn">${esc(text)}</span>`;
    function table(rows) {
      return `<table class="kv"><tbody>${rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${v}</td></tr>`).join("")}</tbody></table>`;
    }
    function bool(v) { return pill(Boolean(v), v ? "yes" : "no"); }
    function json(v) { return `<span class="mono">${esc(JSON.stringify(v, null, 2))}</span>`; }
    async function loadStatus() {
      const res = await fetch('/debug/api/status', { cache: 'no-store' });
      const data = await res.json();
      $('stamp').textContent = new Date(data.generated_at * 1000).toLocaleString();
      $('runtime').innerHTML = table([
        ['pid', esc(data.runtime.pid)],
        ['uptime', esc(`${data.runtime.uptime_seconds}s`)],
        ['python', esc(data.runtime.python)],
        ['platform', esc(data.runtime.platform)]
      ]);
      $('bot').innerHTML = table([
        ['running', bool(data.bot.running)],
        ['chat ready', bool(data.bot.chat_window_ready)],
        ['account', esc(data.bot.user.account || '')],
        ['nickname', esc(data.bot.user.nickname || '')],
        ['version', esc(data.bot.user.version || '')]
      ]);
      $('queues').innerHTML = table([
        ['message', esc(data.queues.message_queue.size)],
        ['rpa', esc(data.queues.rpa_task_queue.size)],
        ['status', esc(data.queues.status_queue.size)]
      ]);
      $('vision').innerHTML = table([
        ['yolo', bool(data.yolo.model_loaded)],
        ['imgsz config', esc(data.yolo.imgsz_config || '')],
        ['imgsz actual', esc(JSON.stringify(data.yolo.imgsz_actual || ''))],
        ['stride', esc(data.yolo.stride || '')],
        ['visual running', bool(data.services.visual.running)],
        ['primed', bool(data.services.visual.primed)],
        ['session', esc(data.services.visual.current_session || '')],
        ['seen hashes', esc(data.services.visual.seen_visual_hashes || 0)]
      ]);
      $('database').innerHTML = table([
        ['root', esc(data.database.root || '')],
        ['accounts', esc(data.database.account_count || 0)],
        ['sqlcipher', data.database.sqlcipher_available ? pill(true, data.database.sqlcipher_driver || 'available') : warn('missing')],
        ['open ready', bool(data.database.can_attempt_business_database_open)]
      ]) + `<div class="list">${(data.database.accounts || []).map(a => `<div class="rowline"><span>${esc(a.account)}</span><span class="mono">${esc(a.encrypted_databases)} enc / ${esc(a.plaintext_databases)} plain</span></div>`).join("")}</div>`;
      $('services').innerHTML = table([
        ['processor', bool(data.services.processor.is_running)],
        ['processor queue', esc(data.services.processor.queue_size ?? '')],
        ['rpa', bool(data.services.rpa.is_running)],
        ['rpa queue', esc(data.services.rpa.queue_size ?? '')],
        ['mqtt', data.services.mqtt.enabled ? bool(data.services.mqtt.connected) : warn('disabled')]
      ]);
      $('processes').innerHTML = table([
        ['wechat', bool(data.processes.wechat.running)],
        ['wechat pids', esc((data.processes.wechat.pids || []).join(', '))],
        ['bot', bool(data.processes.bot.running)],
        ['display', data.processes.display.ok ? pill(true, data.processes.display.stdout) : warn(data.processes.display.stderr || 'unknown')]
      ]);
      $('plugins').innerHTML = `<div class="list">${(data.plugins || []).map(p => `<div class="rowline"><span>${esc(p.name)}</span><span class="mono">${esc(p.priority)} ${esc(p.class)}</span></div>`).join("") || '<span class="muted">none</span>'}</div>`;
      $('layout').src = `/debug/layout.png?t=${Date.now()}`;
    }
    async function loadLogs() {
      const res = await fetch('/debug/api/logs?lines=180', { cache: 'no-store' });
      const data = await res.json();
      $('logText').textContent = data.text || '';
    }
    $('refresh').addEventListener('click', loadStatus);
    $('logs').addEventListener('click', loadLogs);
    loadStatus().catch(err => { $('stamp').textContent = err.message; });
    loadLogs().catch(() => {});
    setInterval(loadStatus, 5000);
  </script>
</body>
</html>
"""
