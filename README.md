# WeChat-AI

Docker 容器化 Linux 微信 + AI Bot，基于 Selkies WebRTC 浏览器访问，集成 omni-bot-sdk 插件系统和 MCP Server。

## 项目简介

在 Docker 容器中同时运行：
- **Linux 微信客户端**（通过 Selkies WebRTC 在浏览器中远程使用）
- **AI 机器人**（OCR+YOLO 视觉识别 + 插件系统 + MCP Server）

适用于服务器部署、远程办公、微信自动回复等场景。

## 快速开始

### 环境要求

- Docker & Docker Compose
- 支持 WebRTC 的浏览器（Chrome/Firefox/Safari）
- （可选）GPU 硬件加速：`/dev/dri` 设备

### 部署

```bash
# 1. 进入项目目录
cd wechat-ai

# 2. （可选）复制并修改配置
cp .env.example .env

# 3. 构建并启动
docker compose up -d --build

# 4. 访问微信
# 浏览器打开: https://localhost:3001
```

### 配置

```bash
# 编辑 bot 配置
vim config/config.yaml

# 主要配置项:
# - wechat_user: 微信用户信息
# - mcp.port: MCP Server 端口 (默认 8000)
# - mqtt: MQTT 消息转发 (可选)
# - plugins: 插件启用/禁用
# - openai: LLM API 配置
# - rpa: RPA 操作参数
# - visual_message: 视觉消息读取参数
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HTTP_PORT` | 3000 | HTTP 访问端口 |
| `HTTPS_PORT` | 3001 | HTTPS 访问端口 |
| `MCP_PORT` | 8000 | MCP Server 端口 |
| `AUTO_START_WECHAT` | true | 自动启动微信 |
| `BOT_ENABLED` | true | 启动 AI Bot |
| `CUSTOM_USER` | - | Selkies Web UI 用户名 |
| `PASSWORD` | - | Selkies Web UI 密码 |
| `SHM_SIZE` | 2gb | 共享内存大小 |

## 架构

```
Container: wechat-ai
+----------------------------------------------------+
|  Selkies WebRTC (ports 3000/3001)                  |
|  +-----------+  +-----------+  +----------------+  |
|  | Openbox WM|  | WeChat    |  | stalonetray    |  |
|  | (X11)     |  | (Linux)   |  +----------------+  |
|  +-----------+  +-----------+                      |
+----------------------------------------------------+
|  AI Bot Process (Python 3.12)                      |
|  +---------------+  +----------+  +------------+   |
|  | PluginManager |  | MCP Svr  |  | MQTT Client|   |
|  +-------+-------+  +----+-----+  +-----+------+   |
|          |                |              |          |
|  +-------v----------------v--------------v------+   |
|  |         ProcessorService                    |   |
|  +----+-----------------------------+---------+   |
|       |                             |              |
|  +----v------+              +-------v--------+     |
|  | Linux RPA |              | Visual Message |     |
|  | Layer     |              | Reader (OCR)   |     |
|  | (X11/pyau-|              | - Screenshot   |     |
|  | togui/mss)|              |   chat area    |     |
|  +-----------+              | - YOLO + OCR   |     |
|                              +----------------+     |
+----------------------------------------------------+
```

## Linux 微信数据库状态

当前实时消息读取走视觉通道。Linux 微信 4.x 的数据位于
`/config/xwechat_files/<account>_<suffix>/db_storage/`，按
`message/contact/session/...` 分库；这些业务 `.db` 文件不是普通
`SQLite format 3` 文件头，标准库 `sqlite3` 不能直接打开。

`/config/xwechat_files/all_users/login/<account>/key_info.db` 是明文 SQLite，
包含 `LoginKeyInfoTable(user_name_md5, key_md5, key_info_md5, key_info_data)`。
项目已加入 `LinuxDatabaseDiscovery` 用于只读发现账号目录、业务库路径和 key
元数据形态。后续接入 SQLCipher/WCDB 打开业务库后，再替换或补充视觉读取。

## 技术栈

- **基础镜像**: `ghcr.io/linuxserver/baseimage-selkies:ubuntunoble`
- **微信**: 官方 Linux 版 (4.1.x)
- **远程访问**: Selkies WebRTC
- **窗口管理**: Python Xlib + Openbox
- **AI/OCR**: RapidOCR + Ultralytics YOLO
- **消息协议**: Protocol Buffers + XML + Zstandard
- **LLM 集成**: MCP Server (FastMCP)
- **消息转发**: MQTT (Paho)

## MCP Server

Bot 启动后 MCP Server 监听 `http://localhost:8000`，提供以下工具：
- `send_text_msg` - 发送文本消息
- `send_file_msg` - 发送文件
- `send_pat_msg` - 发送拍一拍
- `leave_room` - 退出群聊
- `public_room_announcement` - 发布群公告
- `rename_room_name` - 修改群名
- `remove_room_member` - 移除群成员
- `invite_room_member` - 邀请入群
- 更多工具见 `mcp/app.py`

## 项目结构

```
wechat-ai/
├── Dockerfile                    # 容器构建
├── docker-compose.yml            # 部署配置
├── config.example.yaml           # Bot 配置模板
├── pyproject.toml                # Python 包定义
├── root/                         # 容器初始化脚本
│   ├── defaults/{autostart,menu.xml}
│   └── scripts/{start.sh,start-bot.sh,health-check.sh}
├── src/wechat_ai_bot/            # Bot 源码
│   ├── bot.py                    # 主 Bot 类
│   ├── rpa/                      # Linux RPA 层 (X11)
│   ├── plugins/                  # 插件系统
│   ├── mcp/                      # MCP Server
│   ├── weixin/                   # 消息解析
│   └── services/                 # 核心服务
├── plugins/                      # 用户插件目录
└── config/                       # 运行时数据 (/config)
```

## 健康检查

```bash
docker exec wechat-ai /scripts/health-check.sh
```

## 日志

```bash
# 查看容器日志
docker compose logs -f wechat-ai

# 查看 bot 日志
tail -f config/logs/bot.log
```

## 升级微信

```bash
docker compose build --no-cache
docker compose up -d
```

## 许可证

MIT License
