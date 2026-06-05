#!/bin/bash
# WeChat-AI init for Webtop XFCE (runs via /custom-cont-init.d)
BOT_LOG="/config/logs/bot.log"
mkdir -p /config/logs /config/runtime_images /config/.config/autostart

# Fix permissions for abc user (bot runs as abc)
chown -R abc:abc /config/logs /config/runtime_images /config/.config/ 2>/dev/null || true

echo "[bot] $(date): Setting up WeChat-AI..." | tee -a "$BOT_LOG"

# Launch wrapper for WeChat (XFCE X11)
cat > /scripts/launch-wechat.sh << 'LAUNCH'
#!/bin/bash
export DISPLAY=:1
exec /usr/bin/wechat
LAUNCH
chmod +x /scripts/launch-wechat.sh

# XFCE autostart for WeChat
cat > /config/.config/autostart/wechat.desktop << 'EOF'
[Desktop Entry]
Type=Application
Name=WeChat
Exec=/scripts/launch-wechat.sh
EOF

# XFCE autostart for bot
cat > /config/.config/autostart/wechat-ai-bot.desktop << 'EOF'
[Desktop Entry]
Type=Application
Name=WeChat-AI Bot
Exec=/scripts/start-bot.sh
Terminal=false
EOF

chown abc:abc /config/.config/autostart/*.desktop 2>/dev/null || true

# Wait for XFCE desktop and launch WeChat immediately
(
    for i in $(seq 1 30); do
        if xdpyinfo -display :1 >/dev/null 2>&1; then
            sleep 3
            echo "[bot] $(date): XFCE ready, launching WeChat..." | tee -a "$BOT_LOG"
            sudo -u abc env DISPLAY=:1 /scripts/launch-wechat.sh > /dev/null 2>&1 &
            exit 0
        fi
        sleep 2
    done
    echo "[bot] $(date): Timeout waiting for XFCE" | tee -a "$BOT_LOG"
) &

echo "[bot] $(date): Init complete" | tee -a "$BOT_LOG"
