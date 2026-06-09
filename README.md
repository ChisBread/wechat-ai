# WeChat-AI

> Docker 化的 Linux 微信自动化运行时：Selkies 远程桌面、SQLCipher 数据库读取、视觉 RPA、插件系统和面向 AI Agent 的 MCP Server。

WeChat-AI 适合自托管场景：你已经在容器里的 Linux 微信登录账号，希望 AI 能安全地读取最近聊天、解析联系人，并在确认后执行少量可控的微信 RPA 操作。

[English README](README.en.md)

## 你能用它做什么

- **远程使用 Linux 微信**：通过 Selkies WebRTC 在浏览器里访问容器桌面。
- **内部管理面板**：查看 bot、RPA、数据库、窗口、YOLO、队列、插件和日志状态。
- **读取微信数据库**：使用 Python 版 `sqlcipher3-binary` 打开 Linux 微信 4.x SQLCipher 数据库，读取文本、图片、视频、文件等消息元数据。
- **视觉 RPA**：通过 X11/pyautogui 操作微信窗口，并对窗口尺寸和 YOLO 输入尺寸做对齐，降低分辨率漂移带来的误识别。
- **MCP 接入 AI 客户端**：提供 Streamable HTTP MCP endpoint，可接 Claude Code、OpenClaw 以及其他 MCP 客户端。
- **插件机制**：保留从上游 bot 迁移来的 `wechat_ai.plugins` entry point 插件模型。

## 快速开始

### 1. 启动容器

```bash
docker compose up -d --build
```

默认宿主机端口：

| 服务 | 地址 |
| --- | --- |
| 微信远程桌面 | `https://localhost:3101` |
| 管理面板 | `http://localhost:8100/dashboard` |
| MCP Streamable HTTP | `http://localhost:8100/mcp` |

容器内 MCP 监听 `8000`，compose 默认映射到宿主机 `8100`。如果复制 `.env.example` 到 `.env`，默认值仍保持一致。

### 2. 登录微信

打开 `https://localhost:3101`，登录 Linux 微信，并保持会话在线。微信数据目录通常在：

```text
/config/xwechat_files/<account>_<suffix>/
```

### 3. 检查运行状态

打开：

```text
http://localhost:8100/dashboard
```

重点确认：

- `Database` 可用，并且 contacts/message tables 非 0。
- `Bot`、`RPA`、`MessageService` 正在运行。
- `Window` 能识别当前微信窗口尺寸和消息区域。
- YOLO/RPA 使用的窗口尺寸稳定，例如当前默认 1008px 宽并按 stride 对齐。

### 4. 连接 MCP 客户端

MCP endpoint：

```text
http://localhost:8100/mcp
```

推荐首次调用顺序：

1. `get_runtime_status`
2. `search_contacts`
3. `get_recent_messages` 或 `get_chat_summary_context`
4. `send_text_msg` 且设置 `dry_run=true`
5. 人类确认联系人和文本后，再调用 `send_text_msg`

完整工具说明、Claude Code/OpenClaw 配置示例和安全约束见 [MCP 使用指南](docs/mcp.md)。

## MCP 能力边界

MCP Server 同时提供读库工具和少量已经在 Linux RPA 侧跑通的写操作。

读/状态工具：

- `get_runtime_status`
- `get_wechat_user_info`
- `search_contacts`
- `get_recent_chats`
- `get_recent_messages`
- `get_chat_summary_context`
- `query_wechat_msg`
- `query_room_member_list`

已移植写操作：

- `send_text_msg`
- `public_room_announcement`
- `leave_room`

尚未移植的 Linux RPA 工具会返回结构化 `status: "unavailable"`，不会假装提交成功：

- `send_file_msg`
- `send_pat_msg`
- `remove_room_member`
- `invite_room_member`
- `rename_room_name`
- `rename_name_in_room`

## 管理面板

管理面板挂载在 MCP HTTP 服务上：

```text
http://localhost:8100/dashboard
```

功能包括：

- Overview：bot、服务、队列、进程、插件和 YOLO 状态。
- Database：SQLCipher key 扫描、数据库发现、联系人/消息表状态、重新扫描。
- Messages：联系人搜索、文本历史、图片/视频/文件消息解析检查。
- RPA：文本消息入队。
- Window：不含聊天内容的窗口布局图和消息区域几何信息。
- Logs：脱敏日志尾部。

管理面板没有内置鉴权。对外暴露前必须放到反向代理、登录鉴权和网络访问控制之后。旧入口 `/debug` 会 307 跳转到 `/dashboard`，旧的 `/debug/api/...` 仍兼容。

## 数据库读取

Linux 微信 4.x 数据库位于：

```text
/config/xwechat_files/<account>_<suffix>/db_storage/
```

业务 `.db` 是 SQLCipher 数据库，标准库 `sqlite3` 不能直接打开。本项目使用 Python 驱动 `sqlcipher3-binary`：

- 启动时只读扫描账号目录。
- 从 WeChat 进程内存中寻找 SQLCipher raw key。
- 用数据库首页 HMAC 验证 key。
- key 只保存在 bot 进程内存，不落盘。
- 成功后通过 `MessageService` 轮询 `Msg_*` 表的新消息。

bot 由 s6 作为 root 服务启动，是为了读取 `/proc/<wechat-pid>/mem`。WeChat、X11 和 Selkies 桌面会话仍按容器桌面用户运行。

## 配置

主要文件：

| 文件 | 作用 |
| --- | --- |
| `.env` | 宿主机端口、容器开关 |
| `config/config.yaml` | 运行时 bot 配置 |
| `config.example.yaml` | 新配置模板 |
| `docker-compose.yml` | 容器定义 |

关键配置：

- `database.enabled`：启用 Linux 微信数据库读取。
- `database.scan_keys`：扫描微信进程内存中的 SQLCipher key。
- `rpa.window.*`：稳定微信窗口尺寸，服务 RPA 和 YOLO 对齐。
- `visual_message.*`：OCR/YOLO 视觉读取兜底配置。
- `mcp.host` / `mcp.port`：容器内 MCP 监听地址和端口。
- `mqtt.host`：可选的旧式/远程 MQTT 转发；留空即关闭。

## 架构

```text
Browser
  |
  | HTTPS/WebRTC
  v
Selkies desktop (Openbox + Linux WeChat)
  |
  | X11 screenshot / input automation
  v
Bot runtime
  |-- LinuxDatabaseService -> SQLCipher WeChat DBs
  |-- MessageService       -> DB polling and message factory
  |-- VisualMessageService -> OCR/YOLO fallback
  |-- RPAService           -> local RPA action queue
  |-- PluginManager        -> plugin entry points
  `-- FastMCP             -> /mcp and /dashboard
```

MCP 和 bot 在同一进程运行时，写操作默认直接进入本地 `rpa_task_queue`，不再依赖 MQTT。只有显式配置 `mqtt.host` 且没有本地 bot 队列时，才会走 MQTT dispatcher。

## 开发与测试

容器内运行测试：

```bash
docker exec wechat-ai bash -lc 'cd /app && PYTHONPATH=/app/src /opt/venv-bot/bin/python -m unittest discover -s /app/tests -t /app -v'
```

常用检查：

```bash
docker exec wechat-ai /scripts/health-check.sh
docker compose logs -f wechat-ai
tail -f config/logs/bot.log
```

重新构建并使用现有 `./config` 测试：

```bash
docker compose build wechat-ai
docker compose up -d --force-recreate wechat-ai
```

推送到 GitHub 后，GitHub Actions 会构建并发布 amd64 镜像到 `ghcr.io/chisbread/wechat-ai`。

## 文档

- [MCP 使用指南](docs/mcp.md)
- [Claude Code 接入](docs/clients/claude-code.md)
- [OpenClaw 接入](docs/clients/openclaw.md)
- [微信操作员 Prompt](docs/prompts/wechat-operator.md)
- [只读分析 Prompt](docs/prompts/wechat-readonly-analyst.md)
- [WeChat-AI Skill 模板](docs/skills/wechat-ai/SKILL.md)

## License And Credits

本项目以 **GPL-3.0-or-later** 开源。

项目集成和移植了 `omni-bot-sdk-oss` 的 bot/RPA/插件/消息解析思路及部分代码，因此遵循其 GPL-3.0-or-later 授权要求。容器桌面与 Selkies/微信封装方案参考 `wechat-selkies`，该项目采用 MIT License。

详见 [LICENSE](LICENSE) 和 [CREDITS.md](CREDITS.md)。
