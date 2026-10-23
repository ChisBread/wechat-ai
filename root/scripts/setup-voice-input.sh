#!/bin/bash
# WeChat caches the input-device list at startup. Selkies only supplies
# monitor sources, which WeChat excludes; expose its input as a microphone.
export PULSE_SERVER="${PULSE_SERVER:-unix:/defaults/native}"

has_voice_input() {
    pactl list short sources 2>/dev/null | awk '$2 == "wechat_ai_input" { found=1 } END { exit !found }'
}

for attempt in $(seq 1 20); do
    if has_voice_input; then
        break
    fi
    if pactl load-module module-remap-source master=input.monitor \
        source_name=wechat_ai_input \
        source_properties=device.description=WeChat-AI-Input >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

if ! has_voice_input; then
    echo "[wechat-ai] Could not initialize voice input before WeChat startup" >&2
    exit 1
fi

# Preserve a user-selected microphone; replace only the container's monitor.
case "$(pactl get-default-source 2>/dev/null)" in
    output.monitor|input.monitor) pactl set-default-source wechat_ai_input ;;
esac
