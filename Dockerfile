# syntax=docker/dockerfile:1
# WeChat-AI on Webtop (Ubuntu KDE + Wayland) with Python 3.12 bot
FROM lscr.io/linuxserver/webtop:ubuntu-xfce

LABEL org.opencontainers.image.title="WeChat-AI"

ARG TARGETPLATFORM
ARG BUILDPLATFORM

# Install Python 3.12 alongside system Python 3.14 (for bot compatibility)
RUN apt-get update && \
    apt-get install -y software-properties-common && \
    add-apt-repository -y ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y python3.12 python3.12-venv python3.12-dev

# Create bot virtual environment with Python 3.12
RUN python3.12 -m venv /opt/venv-bot && \
    /opt/venv-bot/bin/pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && \
    /opt/venv-bot/bin/pip install --upgrade pip setuptools wheel

# Install system tools
RUN apt-get install -y \
    grim grimshot curl wget xclip fonts-noto-cjk libgl1

# Install WeChat Linux
RUN case "$TARGETPLATFORM" in \
    "linux/amd64") \
        WECHAT_URL="https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_x86_64.deb" ;; \
    "linux/arm64") \
        WECHAT_URL="https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_arm64.deb" ;; \
    *) echo "Unsupported" >&2; exit 1 ;; \
    esac && \
    curl -fsSL --retry 3 --retry-delay 10 -o wechat.deb "$WECHAT_URL" && \
    (dpkg -i wechat.deb || (apt-get update && apt-get install -f -y && dpkg -i wechat.deb)) && \
    rm -f wechat.deb

# Install bot Python deps (into 3.12 venv)
COPY requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    /opt/venv-bot/bin/pip install -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

# Clean up
RUN apt-get purge -y --autoremove || true
RUN apt-get autoclean && rm -rf /var/lib/apt/lists/* /var/tmp/* /tmp/*

# Environment
ENV TITLE="WeChat-AI"
ENV TZ="Asia/Shanghai"
ENV AUTO_START_WECHAT="true"
ENV BOT_ENABLED="true"
ENV BOT_CONFIG_PATH="/config/config.yaml"
ENV MCP_PORT="8000"

# Copy and install bot package
COPY pyproject.toml /app/
COPY src/ /app/src/
COPY config.example.yaml /app/config.example.yaml
RUN --mount=type=cache,target=/root/.cache/pip \
    cd /app && /opt/venv-bot/bin/pip install -e .

# Init scripts
COPY /root /
RUN chmod +x /scripts/*.sh 2>/dev/null; \
    mkdir -p /custom-cont-init.d && \
    cp /scripts/autostart-bot.sh /custom-cont-init.d/ 2>/dev/null
