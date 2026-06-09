#!/bin/bash
# Start bot in the Selkies/Openbox X11 session.
BOT_LOG="/config/logs/bot.log"
BOT_PYTHON="/opt/venv-bot/bin/python"
BOT_CONFIG="${BOT_CONFIG_PATH:-/config/config.yaml}"
BOT_USER="${BOT_RUN_USER:-root}"

mkdir -p /config/logs /config/runtime_images /config/.config/Ultralytics

echo "[bot] $(date): Waiting for WeChat..." | tee -a "$BOT_LOG"

# Wait for WeChat process
for i in $(seq 1 30); do
    if pgrep -f "wechat" > /dev/null 2>&1; then
        echo "[bot] $(date): WeChat ready" | tee -a "$BOT_LOG"
        break
    fi
    sleep 2
done

# Ensure default config exists
[ ! -f "$BOT_CONFIG" ] && cp /app/config.example.yaml "$BOT_CONFIG"

cd /app
while true; do
    echo "[bot] $(date): Starting bot as ${BOT_USER} on DISPLAY=${DISPLAY:-unset}..." | tee -a "$BOT_LOG"
    if [ "$BOT_USER" = "root" ]; then
        RUN_PREFIX=()
    else
        RUN_PREFIX=(sudo -u "$BOT_USER")
    fi
    "${RUN_PREFIX[@]}" env \
        HOME="${HOME:-/config}" \
        DISPLAY="${DISPLAY:-:1}" \
        XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/config/.XDG}" \
        QT_AUTO_SCREEN_SCALE_FACTOR="${QT_AUTO_SCREEN_SCALE_FACTOR:-0}" \
        QT_SCALE_FACTOR="${QT_SCALE_FACTOR:-1}" \
        QT_FONT_DPI="${QT_FONT_DPI:-96}" \
        MCP_PORT="${MCP_PORT:-8000}" \
        YOLO_CONFIG_DIR="${YOLO_CONFIG_DIR:-/config/.config/Ultralytics}" \
        "$BOT_PYTHON" -m wechat_ai_bot.bot --config "$BOT_CONFIG" 2>&1 | tee -a "$BOT_LOG"
    echo "[bot] $(date): Bot exited, restarting in 10s..." | tee -a "$BOT_LOG"
    sleep 10
done
