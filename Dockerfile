# syntax=docker/dockerfile:1
# WeChat-AI on LinuxServer Selkies + Openbox.
FROM ghcr.io/linuxserver/baseimage-selkies:ubuntunoble

LABEL org.opencontainers.image.title="WeChat-AI"
LABEL org.opencontainers.image.description="Linux WeChat with Selkies WebRTC and WeChat-AI Bot"

ARG TARGETPLATFORM
ARG BUILDPLATFORM
ARG APT_MIRROR="https://mirrors.tuna.tsinghua.edu.cn/ubuntu"
ARG PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
ARG PYTORCH_FIND_LINKS="https://mirrors.aliyun.com/pytorch-wheels/cpu/"

RUN echo "Building WeChat-AI on ${BUILDPLATFORM}, targeting ${TARGETPLATFORM}"

RUN if [ -n "${APT_MIRROR}" ]; then \
        sed -i "s|http://archive.ubuntu.com/ubuntu|${APT_MIRROR}|g; s|http://security.ubuntu.com/ubuntu|${APT_MIRROR}|g; s|https://archive.ubuntu.com/ubuntu|${APT_MIRROR}|g; s|https://security.ubuntu.com/ubuntu|${APT_MIRROR}|g" /etc/apt/sources.list /etc/apt/sources.list.d/*.list 2>/dev/null || true; \
    fi

# WeChat runtime, Openbox helpers, RPA tools, and Python 3.12 for the bot.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
        libxcb-xkb1 libxkbcommon-x11-0 libxcb1 libxcb-randr0 libxcb-render0 \
        libxcb-shape0 libxcb-shm0 libxcb-sync1 libxcb-util1 libxcb-xfixes0 \
        libxcb-xinerama0 libxcb-glx0 libatk1.0-0 libatk-bridge2.0-0 libcairo2 \
        libcups2 libdbus-1-3 libfontconfig1 libgbm1 libgdk-pixbuf2.0-0 \
        libglib2.0-0 libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 \
        libpangocairo-1.0-0 libx11-6 libx11-xcb1 libxcomposite1 libxdamage1 \
        libxext6 libxfixes3 libxi6 libxrandr2 libxrender1 libxss1 libxtst6 \
        libatomic1 shared-mime-info desktop-file-utils stalonetray inotify-tools \
        curl wget xclip xdotool x11-utils x11-xserver-utils gnome-screenshot libgl1 \
        python3.12 python3.12-venv python3.12-dev python3-tk

RUN sed -i 's/^# *zh_CN.UTF-8 UTF-8/zh_CN.UTF-8 UTF-8/' /etc/locale.gen && \
    locale-gen zh_CN.UTF-8

RUN python3.12 -m venv /opt/venv-bot && \
    /opt/venv-bot/bin/pip config set global.index-url "${PIP_INDEX_URL}" && \
    /opt/venv-bot/bin/pip install --upgrade pip setuptools wheel

# Install WeChat based on target architecture.
RUN case "$TARGETPLATFORM" in \
    "linux/amd64") \
        WECHAT_URL="https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_x86_64.deb"; \
        WECHAT_ARCH="x86_64" ;; \
    "linux/arm64") \
        WECHAT_URL="https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_arm64.deb"; \
        WECHAT_ARCH="arm64" ;; \
    *) \
        echo "Unsupported platform: ${TARGETPLATFORM}" >&2; \
        exit 1 ;; \
    esac && \
    echo "Downloading WeChat for ${WECHAT_ARCH}..." && \
    curl -fsSL --retry 3 --retry-delay 10 --retry-all-errors -o /tmp/wechat.deb "$WECHAT_URL" && \
    (dpkg -i /tmp/wechat.deb || (apt-get update && apt-get install -f -y && dpkg -i /tmp/wechat.deb)) && \
    rm -f /tmp/wechat.deb

COPY requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    /opt/venv-bot/bin/pip install --find-links "${PYTORCH_FIND_LINKS}" -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

COPY pyproject.toml /app/
COPY README.md README.en.md LICENSE CREDITS.md /app/
COPY docs/ /app/docs/
COPY src/ /app/src/
COPY tests/ /app/tests/
COPY config.example.yaml /app/config.example.yaml
RUN --mount=type=cache,target=/root/.cache/pip \
    cd /app && /opt/venv-bot/bin/pip install --no-deps -e .

# Keep stalonetray from reserving desktop space.
RUN sed -i '/<dock>/,/<\/dock>/s/<noStrut>no<\/noStrut>/<noStrut>yes<\/noStrut>/' /etc/xdg/openbox/rc.xml

ENV TITLE="WeChat-AI"
ENV TZ="Asia/Shanghai"
ENV LANG="zh_CN.UTF-8"
ENV LANGUAGE="zh_CN:zh"
ENV LC_ALL="zh_CN.UTF-8"
ENV AUTO_START_WECHAT="true"
ENV BOT_ENABLED="true"
ENV BOT_CONFIG_PATH="/config/config.yaml"
ENV MCP_PORT="8000"
ENV SELKIES_MANUAL_WIDTH="1920"
ENV SELKIES_MANUAL_HEIGHT="1916"
ENV QT_AUTO_SCREEN_SCALE_FACTOR="0"
ENV QT_SCALE_FACTOR="1"
ENV QT_FONT_DPI="96"
ENV YOLO_CONFIG_DIR="/config/.config/Ultralytics"

RUN if [ -f /usr/share/icons/hicolor/128x128/apps/wechat.png ]; then \
        cp /usr/share/icons/hicolor/128x128/apps/wechat.png /usr/share/selkies/www/icon.png; \
    fi

COPY /root /
RUN chmod +x /scripts/*.sh /etc/s6-overlay/s6-rc.d/svc-wechat-ai/run 2>/dev/null || true

RUN apt-get autoclean && \
    rm -rf /var/lib/apt/lists/* /var/tmp/* /tmp/*
