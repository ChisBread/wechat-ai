#!/bin/bash

BOT_LOG="/config/logs/bot.log"
mkdir -p /config/logs /config/runtime_images /config/.config/Ultralytics
chown -R abc:users /config/logs /config/runtime_images /config/.config/Ultralytics 2>/dev/null || true
chmod -R u+rwX,g+rwX /config/logs /config/runtime_images /config/.config/Ultralytics 2>/dev/null || true

log_line() {
    local message="$1"
    if ! echo "$message" | tee -a "$BOT_LOG" >/dev/null 2>&1; then
        echo "$message"
    fi
}

reconfigure_openbox() {
    timeout 3 openbox --reconfigure >/dev/null 2>&1 || true
}

# clean up stale dbus pid file to prevent startup failures after container restart
rm -f /run/dbus/pid

# configure openbox dock mode for stalonetray
if [ ! -f /config/.config/openbox/rc.xml ] || grep -A20 "<dock>" /config/.config/openbox/rc.xml | grep -q "<noStrut>no</noStrut>"; then
    mkdir -p /config/.config/openbox
    [ ! -f /config/.config/openbox/rc.xml ] && cp /etc/xdg/openbox/rc.xml /config/.config/openbox/
    sed -i '/<dock>/,/<\/dock>/s/<noStrut>no<\/noStrut>/<noStrut>yes<\/noStrut>/' /config/.config/openbox/rc.xml
    reconfigure_openbox
fi

# configure default window behavior: open WeChat as normal windows instead of maximized
OB_RC="/config/.config/openbox/rc.xml"
if [ -f "$OB_RC" ] && ! grep -q '<application class="wechat"' "$OB_RC"; then
    sed -i '/<\/openbox_config>/i \
  <applications>\
    <application class="wechat">\
      <maximized>no</maximized>\
    </application>\
  </applications>' "$OB_RC"
    reconfigure_openbox
fi

# generate openbox menu from defaults + ~/Desktop/*.desktop files
/scripts/refresh-menu.sh

# watch ~/Desktop/ for .desktop file changes and auto-refresh menu
mkdir -p "$HOME/Desktop"
if command -v inotifywait >/dev/null 2>&1; then
    nohup bash -c '
        watch_dir="$1"
        while inotifywait -q -e create -e delete -e modify "$watch_dir" --include "\\.desktop$"; do
            sleep 1
            /scripts/refresh-menu.sh
        done
    ' _ "$HOME/Desktop/" >/dev/null 2>&1 &
fi

if ! pgrep -x stalonetray >/dev/null 2>&1; then
    nohup stalonetray --dockapp-mode simple > /dev/null 2>&1 &
fi

# start WeChat application in the background if exists and auto-start enabled
if [ "${AUTO_START_WECHAT:-true}" = "true" ]; then
    if [ -f /usr/bin/wechat ]; then
        log_line "[wechat-ai] $(date): launching WeChat"
        nohup /usr/bin/wechat > /config/logs/wechat.log 2>&1 &
    fi
else
    log_line "[wechat-ai] $(date): AUTO_START_WECHAT is disabled"
fi

# The bot is supervised by s6 as root so SQLCipher key scanning can read
# /proc/<wechat-pid>/mem. This desktop autostart script only owns GUI apps.
