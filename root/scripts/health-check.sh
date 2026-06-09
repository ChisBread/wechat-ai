#!/bin/bash
# Health check script for WeChat-AI container
# Checks: WeChat process, bot process, X11 display

BOT_PID=$(pgrep -f "wechat_ai_bot.bot" 2>/dev/null)
DISPLAY_NAME="${DISPLAY:-:1}"
MCP_PORT="${MCP_PORT:-8000}"
WECHAT_WINDOW=$(DISPLAY="$DISPLAY_NAME" xdotool search --class "wechat" 2>/dev/null | head -1)
DISPLAY_OK=$(xdpyinfo -display "$DISPLAY_NAME" >/dev/null 2>&1 && echo "OK" || echo "FAIL")

STATUS=0

echo "=== WeChat-AI Health Check ==="

# Check X11 display
if [ "$DISPLAY_OK" = "OK" ]; then
    echo "[OK] X11 display $DISPLAY_NAME"
else
    echo "[FAIL] X11 display $DISPLAY_NAME"
    STATUS=1
fi

# Check WeChat window
if [ -n "$WECHAT_WINDOW" ]; then
    echo "[OK] WeChat window found"
else
    echo "[WARN] WeChat window not found"
    STATUS=1
fi

# Check bot process
if [ -n "$BOT_PID" ]; then
    echo "[OK] Bot process running (PID: $BOT_PID)"
else
    echo "[WARN] Bot process not running"
    STATUS=1
fi

# Check Selkies web ports
if ss -tlnp | grep -q ":3000\|:3001"; then
    echo "[OK] Selkies web ports (3000/3001) listening"
else
    echo "[WARN] Selkies web ports not listening"
    STATUS=1
fi

# Check MCP port if bot is running
if [ -n "$BOT_PID" ]; then
    if ss -tlnp | grep -q ":${MCP_PORT}"; then
        echo "[OK] MCP server port (${MCP_PORT}) listening"
    else
        echo "[INFO] MCP server not yet listening (may still be starting)"
    fi
fi

echo "=== Health Check: $([ $STATUS -eq 0 ] && echo 'PASS' || echo 'WARN') ==="
exit $STATUS
