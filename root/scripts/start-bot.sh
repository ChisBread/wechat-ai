#!/bin/bash
# Start bot as abc user (has XWayland auth)
BOT_LOG="/config/logs/bot.log"
BOT_PYTHON="/opt/venv-bot/bin/python"

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
[ ! -f /config/config.yaml ] && cp /app/config.example.yaml /config/config.yaml

# Run bot as abc user (has XWayland auth for xdotool/screenshot)
cd /app
while true; do
    echo "[bot] $(date): Starting bot as abc user..." | tee -a "$BOT_LOG"
    sudo -u abc env DISPLAY=:1 XDG_RUNTIME_DIR=/config/.XDG \
        $BOT_PYTHON -m wechat_ai_bot.bot --config /config/config.yaml 2>&1 | tee -a "$BOT_LOG"
    echo "[bot] $(date): Bot exited, restarting in 10s..." | tee -a "$BOT_LOG"
    sleep 10
done
