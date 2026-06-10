#!/bin/bash
# Start bot in the Selkies/Openbox X11 session.
BOT_LOG="/config/logs/bot.log"
BOT_PYTHON="/opt/venv-bot/bin/python"
BOT_CONFIG="${BOT_CONFIG_PATH:-/config/config.yaml}"
BOT_USER="${BOT_RUN_USER:-root}"
BOT_CHILD_PID=""

stop_bot_children() {
    if [ -n "$BOT_CHILD_PID" ]; then
        kill -TERM "-$BOT_CHILD_PID" 2>/dev/null || kill -TERM "$BOT_CHILD_PID" 2>/dev/null || true
        wait "$BOT_CHILD_PID" 2>/dev/null || true
        BOT_CHILD_PID=""
    fi
    pkill -TERM -f "$BOT_PYTHON -m wechat_ai_bot.bot" 2>/dev/null || true
}

trap 'stop_bot_children; exit 0' TERM INT

mkdir -p /config/logs /config/runtime_images /config/.config/Ultralytics
chown -R abc:users /config/logs /config/runtime_images /config/.config/Ultralytics 2>/dev/null || true
chmod -R u+rwX,g+rwX /config/logs /config/runtime_images /config/.config/Ultralytics 2>/dev/null || true

start_wechat_if_needed() {
    if [ "${AUTO_START_WECHAT:-true}" != "true" ] || pgrep -x "wechat" >/dev/null 2>&1; then
        return
    fi
    if [ -x /usr/bin/wechat ]; then
        echo "[bot] $(date): WeChat not running; launching as abc on DISPLAY=${DISPLAY:-:1}" | tee -a "$BOT_LOG"
        sudo -u abc env \
            HOME=/config \
            DISPLAY="${DISPLAY:-:1}" \
            XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/config/.XDG}" \
            LANG="${LANG:-zh_CN.UTF-8}" \
            LANGUAGE="${LANGUAGE:-zh_CN:zh}" \
            LC_ALL="${LC_ALL:-zh_CN.UTF-8}" \
            QT_AUTO_SCREEN_SCALE_FACTOR="${QT_AUTO_SCREEN_SCALE_FACTOR:-0}" \
            QT_SCALE_FACTOR="${QT_SCALE_FACTOR:-1}" \
            QT_FONT_DPI="${QT_FONT_DPI:-96}" \
            bash -lc 'nohup /usr/bin/wechat > /config/logs/wechat.log 2>&1 &'
    fi
}

echo "[bot] $(date): Waiting for WeChat..." | tee -a "$BOT_LOG"

# Wait for WeChat process
for i in $(seq 1 30); do
    if [ "$i" -eq 1 ] || [ "$i" -eq 10 ]; then
        start_wechat_if_needed
    fi
    if pgrep -x "wechat" > /dev/null 2>&1; then
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
    pkill -TERM -f "$BOT_PYTHON -m wechat_ai_bot.bot" 2>/dev/null && sleep 1
    if command -v xrdb >/dev/null 2>&1; then
        echo "Xft.dpi: 96" > /config/.Xresources
        DISPLAY="${DISPLAY:-:1}" xrdb /config/.Xresources 2>/dev/null || true
    fi
    if command -v xset >/dev/null 2>&1; then
        DISPLAY="${DISPLAY:-:1}" xset +dpms 2>/dev/null || true
    fi
    setsid bash -c '
        bot_user="$1"
        bot_python="$2"
        bot_config="$3"
        shift 3
        if [ "$bot_user" = "root" ]; then
            exec env \
                HOME="${HOME:-/config}" \
                DISPLAY="${DISPLAY:-:1}" \
                XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/config/.XDG}" \
                QT_AUTO_SCREEN_SCALE_FACTOR="${QT_AUTO_SCREEN_SCALE_FACTOR:-0}" \
                QT_SCALE_FACTOR="${QT_SCALE_FACTOR:-1}" \
                QT_FONT_DPI="${QT_FONT_DPI:-96}" \
                MCP_PORT="${MCP_PORT:-8000}" \
                YOLO_CONFIG_DIR="${YOLO_CONFIG_DIR:-/config/.config/Ultralytics}" \
                "$bot_python" -m wechat_ai_bot.bot --config "$bot_config"
        fi
        exec sudo -u "$bot_user" env \
            HOME="${HOME:-/config}" \
            DISPLAY="${DISPLAY:-:1}" \
            XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/config/.XDG}" \
            QT_AUTO_SCREEN_SCALE_FACTOR="${QT_AUTO_SCREEN_SCALE_FACTOR:-0}" \
            QT_SCALE_FACTOR="${QT_SCALE_FACTOR:-1}" \
            QT_FONT_DPI="${QT_FONT_DPI:-96}" \
            MCP_PORT="${MCP_PORT:-8000}" \
            YOLO_CONFIG_DIR="${YOLO_CONFIG_DIR:-/config/.config/Ultralytics}" \
            "$bot_python" -m wechat_ai_bot.bot --config "$bot_config"
    ' _ "$BOT_USER" "$BOT_PYTHON" "$BOT_CONFIG" > >(tee -a "$BOT_LOG") 2>&1 &
    BOT_CHILD_PID=$!
    wait "$BOT_CHILD_PID"
    BOT_CHILD_PID=""
    echo "[bot] $(date): Bot exited, restarting in 10s..." | tee -a "$BOT_LOG"
    sleep 10
done
