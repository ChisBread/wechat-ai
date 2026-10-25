# WeChat-AI

> 一个跑在 Docker 里的 Linux 微信自动化环境，提供远程桌面、微信数据库读取、视觉 RPA、插件系统和面向 AI Agent 的 MCP 服务。

WeChat-AI 主要解决一个问题：把登录好的 Linux 微信放进一个可控的容器里，让程序可以稳定读取微信消息，并在你确认后执行少量微信操作。

它适合自用或内网部署。你可以把它接到 Claude Code、OpenClaw 或其他 MCP 客户端，让 Agent 先查联系人、读上下文、整理回复草稿，再由人确认是否发送。

[English README](README.en.md)

## 能做什么

- **浏览器里使用 Linux 微信**：容器内置 Selkies 远程桌面，打开浏览器就能扫码、登录和操作微信。
- **读取微信本地数据库**：通过 Python 版 `sqlcipher3-binary` 只读打开 Linux 微信 4.x 数据库，读取联系人、文本消息，以及图片、视频、文件等消息元数据。
- **解析本地媒体路径**：能定位图片、视频、文件等本地路径。Linux 微信 4.x 的图片 `.dat` 会识别格式并尝试解密；未配置图片 DAT AES key 时，管理台会返回明确诊断。
- **给 AI 客户端提供 MCP**：默认暴露 Streamable HTTP MCP endpoint，Claude Code、OpenClaw 等客户端可以直接接入。
- **执行受控 RPA 操作**：支持发送文本/文件、拍一拍、群公告、退群、邀请/移除群成员、修改群名和群内昵称。写操作建议先 dry run，再由人确认。
- **管理和调试运行时**：内置管理台可以查看数据库、队列、RPA、窗口尺寸、YOLO、插件和日志状态，也可以一键重扫数据库、重置微信窗口尺寸。
- **保留插件体系**：沿用上游 `wechat_ai.plugins` entry point 模型，方便后续迁移和扩展业务插件。

## 快速开始

### 1. 启动

```bash
docker compose up -d --build
```

默认端口如下：

| 服务 | 地址 |
| --- | --- |
| 微信远程桌面 | `https://localhost:3101` |
| 管理台 | `http://localhost:8100/dashboard` |
| MCP | `http://localhost:8100/mcp` |

如果你复制 `.env.example` 为 `.env`，默认端口仍然是这组值。

第一次启动前请先改 `.env` 里的管理台账号密码：

```dotenv
WECHAT_AI_DASHBOARD_USERNAME=你的用户名
WECHAT_AI_DASHBOARD_PASSWORD=换成强密码
WECHAT_AI_MCP_TOKEN=换成另一串长随机 token
```

`wechat/wechat` 和 `WECHAT_AI_MCP_TOKEN=wechat` 都只是占位值，程序会拒绝用默认值登录或访问 MCP。

### 2. 登录微信

打开：

```text
https://localhost:3101
```

扫码登录 Linux 微信，并保持会话在线。微信数据通常会出现在：

```text
/config/xwechat_files/<account>_<suffix>/
```

### 3. 看管理台

打开：

```text
http://localhost:8100/dashboard
```

浏览器会弹出 Basic Auth 登录框。账号密码来自 `.env` 里的 `WECHAT_AI_DASHBOARD_USERNAME` / `WECHAT_AI_DASHBOARD_PASSWORD`。

重点看这几项：

- 数据库是否可用，联系人数量和消息表数量是否大于 0。
- Bot、RPA、MessageService 是否在运行。
- 微信窗口是否已经对齐到目标尺寸。
- 如果窗口尺寸不对，先点顶部的“重置窗口尺寸”。

当前默认窗口宽度是 `1008px`，高度会按屏幕和 YOLO stride 自动对齐。这样做是为了避免截图分辨率漂移导致 YOLO/RPA 判断失准。

### 4. 接 MCP 客户端

MCP 地址：

```text
http://localhost:8100/mcp
```

MCP 使用 Bearer token 鉴权：

```http
Authorization: Bearer <WECHAT_AI_MCP_TOKEN>
```

建议 Agent 按这个顺序工作：

1. `get_runtime_status`：确认数据库、RPA 和微信窗口状态。
2. `search_contacts` 或 `get_contact_detail`：解析联系人或群聊。
3. `get_recent_messages`、`search_text_messages` 或 `get_chat_summary_context`：读取必要上下文。
4. 调用写操作前先设置 `dry_run=true`，只解析目标和参数，不真正执行。
5. 把收件人、群聊、完整文本或群操作参数展示给人确认。
6. 人明确确认后，再调用对应写工具执行。

MCP 通过 Bearer token 认证后默认只开放读取类工具。需要让 Agent 执行运维或写微信操作时，再按需打开：

```dotenv
WECHAT_AI_MCP_ADMIN_ENABLED=true   # 允许 reset_wechat_window、refresh_database、set_message_polling
WECHAT_AI_MCP_WRITE_ENABLED=true   # 允许 send_text_msg、send_file_msg、群操作等真实写入
```

更完整的工具说明和客户端配置见 [MCP 使用指南](docs/mcp.md)。

## MCP 工具概览

状态和运维：

- `get_runtime_status`：查看 bot、数据库、RPA、队列、窗口、YOLO 等整体状态。
- `get_database_status` / `refresh_database`：查看或重新扫描微信数据库。
- `get_wechat_window_status` / `reset_wechat_window`：查看或重置微信窗口尺寸与布局。
- `discover_dat_keys`：向文件传输助手发送探测图片，自动推断图片 DAT XOR key，并派生/扫描 AES key。
- `set_message_polling`：暂停或恢复数据库消息监听。
- `get_wechat_user_info`：查看当前微信身份信息，敏感字段会脱敏。

联系人和消息读取：

收到语音后可自动调用微信自身的“转文字”：在 `config/config.yaml` 设置
`voice_transcription.enabled: true`（默认关闭），重启 bot 后生效。转写与其他
RPA 操作共用队列，结果按原消息 ID 从数据库回读，交给插件并通过 MCP 返回。
这是微信客户端识别，不是本地 ASR；依赖中文界面的“转文字”菜单与可见的最近语音。
失败或超时保留原语音继续处理，不自动反复点击；详见[语音转写](docs/mcp.md#语音转写)。

- `search_contacts`：按微信 ID、备注、昵称、alias 搜索联系人或群聊。
- `get_contact_detail`：查看单个联系人/群聊详情。
- `get_recent_chats`：列出最近有消息表的会话。
- `get_recent_messages`：读取最近消息，可选择解析媒体路径。
- `search_text_messages`：按关键词和时间范围搜索文本消息。
- `get_recent_media_messages`：读取最近的图片、视频、文件等非文本消息，并尽量解析本地路径。
- `get_chat_summary_context`：返回适合放进 LLM 上下文的简洁聊天记录。
- `query_room_member_list`：读取群成员列表。
- `query_wechat_msg`：旧版文本查询工具，保留兼容。

已经迁移的写操作：

- `send_text_msg`：发送文本消息。参数是 `recipient_name`、`message`、可选 `at_user_name`；群聊 @ 不要手写进正文，应该传 `at_user_name`。默认会等待本地 RPA 执行结果；传 `wait_seconds=0` 可只入队不等待。
- `send_file_msg`：发送容器内可访问的本地文件。
- `send_image_msg`：直接接收图片 base64（也支持 data URI），由服务端临时落盘并按图片发送；支持 PNG/JPEG/WebP/GIF，解码后最大 10 MiB，可先用 `dry_run=true` 校验。
- `send_voice_msg`：接收本地音频路径或 base64，通过虚拟麦克风录制并发送微信原生语音条。输入最大 10 MiB、时长 1–55 秒；需要 Linux 微信 4.1.13 浅色界面、100% 缩放及 PulseAudio，详见[语音发送协议](docs/mcp.md#语音发送)。
- `get_rpa_result`：按发送返回的 `action_id` 查询原任务状态。文本/图片/文件只有回查数据库确认新增外发内容后才返回 `executed`；核验失败返回 `failed`。`queued` / `timeout` 后应查询原任务，禁止自动重发，避免重复消息。
- `send_pat_msg`：对联系人或群内成员执行“拍一拍”。
- `public_room_announcement`：发布或编辑群公告。
- `leave_room`：退群。
- `remove_room_member` / `invite_room_member`：移除或邀请群成员。
- `rename_room_name` / `rename_name_in_room`：修改群名或自己在群内的昵称。

这些写操作都依赖当前微信界面、窗口尺寸、OCR 和 YOLO 识别结果。调用前建议先看 `get_wechat_window_status`，异常时先执行 `reset_wechat_window`。群成员、群名、退群、群公告属于高影响操作，必须由人明确确认；确认后的真实执行调用还需要传 `confirm=true`。完整参数协议、返回状态和群聊 @ 示例见 [MCP 使用指南](docs/mcp.md#参数协议)。

## 管理台

管理台地址：

```text
http://localhost:8100/dashboard
```

它是内部运维页面，内置 Basic Auth。不要直接暴露到公网；如果需要远程访问，请放到 HTTPS 反向代理、额外登录鉴权和访问控制后面。`wechat/wechat` 默认账号密码不能登录，必须在环境变量里改成自己的账号和强密码。

目前管理台提供：

- 总览：bot、队列、服务、进程、插件状态。
- 数据库：SQLCipher key 扫描、账号发现、联系人和消息表状态、手动重扫。
- 消息：联系人搜索、文本消息查询、媒体消息解析检查。
- RPA：手动发送文本消息。
- 窗口：微信窗口几何信息、消息区域、布局示意图、YOLO 状态。
- 日志：脱敏后的 bot 日志尾部。

旧入口 `/debug` 已移除，请统一使用 `/dashboard`。

## 安全默认值

- Dashboard、联系人、消息、媒体代理、日志、窗口控制和 Dashboard RPA API 都需要 Basic Auth。
- Dashboard 的状态变更 POST 还要求 `X-WeChat-AI-Dashboard: 1`，用于降低浏览器 CSRF 风险。
- MCP endpoint 需要 `Authorization: Bearer <WECHAT_AI_MCP_TOKEN>`。
- 默认 `WECHAT_AI_DASHBOARD_USERNAME=wechat` 且 `WECHAT_AI_DASHBOARD_PASSWORD=wechat` 时禁止登录，避免镜像一启动就暴露可猜口令。
- 默认 `WECHAT_AI_MCP_TOKEN=wechat` 时拒绝访问 MCP，避免聊天记录读取接口裸露。
- MCP 认证通过后读取工具可用；`refresh_database`、`reset_wechat_window`、`discover_dat_keys`、`set_message_polling` 需要 `WECHAT_AI_MCP_ADMIN_ENABLED=true`。
- `discover_dat_keys` 如果要自动发送探测图片，还需要 `WECHAT_AI_MCP_WRITE_ENABLED=true`；只扫描已有 probe 可关闭发送。
- MCP 写入微信的工具默认被拦截；发送消息、文件、拍一拍、群公告、退群、邀请/移除群成员、改群名等需要 `WECHAT_AI_MCP_WRITE_ENABLED=true`。
- 退群、群公告、邀请/移除群成员、改群名和群内昵称即使开启写入，也必须在工具参数里显式传 `confirm=true`。
- 管理台会提供联系人、消息、多媒体、日志和本地媒体代理能力，端口不要裸露到公网。
- 如果必须远程访问，请使用 HTTPS、反向代理鉴权、IP 白名单或 VPN。不要把微信数据目录、MCP endpoint 或 Dashboard 直接暴露给不受信任网络。

## 数据库读取说明

Linux 微信 4.x 的业务数据库在：

```text
/config/xwechat_files/<account>_<suffix>/db_storage/
```

这些 `.db` 是 SQLCipher 数据库，不能直接用标准库 `sqlite3` 打开。本项目启动后会：

1. 扫描账号目录和数据库文件。
2. 从微信进程内存中查找 SQLCipher raw key；支持新版微信中按 32 字节循环 XOR 混淆的 raw key 缓冲区。
3. 用数据库首页 HMAC 校验 key。
4. 只读打开数据库，加载联系人、群聊和消息表。
5. 通过 `MessageService` 轮询新消息。

key 只保存在 bot 进程内存中，不写入磁盘。

数据库连接以只读方式打开，每次复用及查询返回前检查主数据库、WAL 和 SHM 的文件身份。文件替换时会丢弃旧连接、重建消息映射并保留已有消费游标。若微信仍向已删除的 WAL 写入，则通过 `/proc/<wechat-pid>/fd` 读取对应日志，在 SQLite WAL 写入/检查点锁保护下生成私有的加密临时快照，随后续提交更新；不覆盖或删除微信源文件。正常 WAL 追加复用连接。数据库状态中的 `connection_replacements` 记录失效连接次数，`orphan_wal_snapshots` 表示当前使用的孤立 WAL 快照数。此恢复依赖同一 PID 命名空间和读取微信文件描述符的权限；容器默认 root 运行可满足。

群消息展示使用当前群的成员昵称，显示名称优先级为联系人备注、群内昵称、普通昵称、用户名。管理台、MCP 和消息处理链统一移除正文开头完整的 `发送者用户名:\n` 前缀，保留正文自身的字符和空白。

bot 服务以 root 运行，是为了读取 `/proc/<wechat-pid>/mem`。微信、X11 和桌面会话仍按容器桌面用户运行。

## 图片和媒体

文本消息、联系人、群成员、文件/视频元数据主要来自数据库。图片消息会先解析本地 `.dat` 路径：

- Linux 微信 4.x 图片 DAT 使用 `V2` 结构：`15 字节头 + PKCS7 padding 后的 AES-ECB 前缀 + 可选 raw 中段 + XOR 尾部`。
- 项目已经能识别 DAT 头、推断常见单字节 XOR key，并在配置了 AES key 时把图片解成 PNG/JPEG/GIF/WebP/BMP/TIFF 返回给管理台。
- `wxgf` 是包含 HEVC 的微信图片封装。Dashboard 和 MCP 会提取码流、通过 FFmpeg 转为 PNG 首帧预览，保留解码尺寸，不把封装数据当成裸 HEVC 返回。Docker 镜像包含 FFmpeg；本机运行需自行安装。动画及透明遮罩不保证保留，返回 `media_conversion=wxgf_first_frame_png` 标明首帧预览。
- `aes_xor_key` 缺失时，启动阶段会尝试用本地图片和 `kvcomm` 派生并验证密钥；Dashboard 和 MCP 解密时也会自动恢复缺失或失效的密钥。验证成功后原子写回配置（权限 0600），重启后直接加载，不发送探测消息。运行状态 `media.dat_key_persisted` 显示是否已保存；失败会返回具体诊断，不再静默省略图片。
- `aes_xor_key` 格式为 `AES文本key,60`；如果你拿到的是十六进制原始 key，用 `hex:<hexkey>,60`。

管理台顶部的“发现图片密钥”和 MCP 工具 `discover_dat_keys` 会自动生成 probe 图片、发送到文件传输助手、定位新生成的 `_h.dat`、推断 XOR key，并优先用 Linux 微信的 `kvcomm/key_<code>_*.statistic` 与账号目录派生 AES key。派生失败时会退回到 WeChat 相关进程内存扫描。

发现成功后会自动写回 `aes_xor_key`，后续直接复用这个持久化 key。若某次媒体解密发现持久化 key 已失效，Dashboard 和 MCP 会先基于当前 DAT 和本地 `kvcomm` 做一次不发送消息的轻量刷新；仍失败时才需要手动点击“发现图片密钥”或调用 `discover_dat_keys(send_probe=true)` 重新发送 probe。

图片读取优先选择 `_h.dat` 原图档，再选择 `.dat` 普通图，最后回退到 `_t.dat` 缩略图。MCP 返回 `media_tier`、`is_thumbnail`、`media_width`、`media_height`、`media_path` 和 `media_url`；降级时返回 `media_fallback_reason`，解密或转换失败还会提供 `media_fallback_errors` / `media_errors`。普通图不能据此认定为原图。

供视觉模型看图时，使用现有 `get_recent_messages(parse_media=true)` 或 `get_recent_media_messages`。响应的第一个内容块为 JSON 元数据，后续为 MCP 原生 `ImageContent`；消息的 `image_content_index` 指向对应内容块，`image_count` 表示实际返回的图片数。图片 base64 仅放在图片内容块中，避免作为大段文本重复传给模型；分辨率、图片档位、媒体地址和失败诊断仍保留在 JSON 中。调用端应转发完整 `content` 列表，而非只读取第一个文本块。普通消息读取（`parse_media=false`）和 Dashboard 返回格式不变。

超过 5 MiB 的解码结果不内嵌 base64，但保留所选图片及受 Dashboard 认证保护的媒体地址，返回 `media_inline_reason=exceeds_5_mib`，不会为满足大小限制偷偷换成缩略图。DAT 解码限制输入为 64 MiB，WXGF 转换限制 20 秒和 4000 万像素。

自动原图下载尚未实现，`DownloadImageHandler` 现在明确返回失败，不再仅切换会话、滚动就报告成功。原图尚未落盘时，可先在微信中打开对应图片并下载原图，再重新读取。

后续 TODO：

- 微信不会把所有历史图片、视频和文件立即落盘；很多媒体只有在 GUI 里浏览到对应消息、点开图片/视频或触发下载后，本地 `msg/attach`、`msg/video` 等目录才会出现完整文件。参考项目目前也没有完整解决：`omni-bot-sdk-oss` 的图片下载 handler 只是切会话并滚动，视频 handler 会点开可见视频，`wechat-decrypt` 只负责解析已经存在的本地 DAT/缓存。
- 需要实现一个“媒体补全”队列：根据数据库定位消息和会话，驱动 GUI 切到对应聊天、滚动到目标消息、点击或预览媒体以触发微信下载，等待文件落盘后再走现有 DAT/文件解析链路。管理台可以给缺失媒体提供“触发下载/重试解析”按钮，MCP 可以提供受限的 `ensure_media_downloaded` 工具。

## 配置

常用文件：

| 文件 | 作用 |
| --- | --- |
| `.env` | 宿主机端口和容器开关 |
| `config/config.yaml` | 运行时配置 |
| `config.example.yaml` | 新配置模板 |
| `docker-compose.yml` | 容器编排 |

常用配置项：

- `database.enabled`：启用数据库读取。
- `database.scan_keys`：扫描微信进程内存中的 SQLCipher key。
- `aes_xor_key`：图片 DAT 解密参数，格式如 `AES文本key,60` 或 `hex:<hexkey>,60`；留空则只解析媒体路径。
- `rpa.window.*`：控制微信窗口目标尺寸，服务 RPA 和 YOLO 对齐。
- 语音发送使用镜像内的 FFmpeg、`pactl` 和 `paplay`，音频在本地处理。镜像通过 `PULSE_SERVER=unix:/defaults/native` 连接 Selkies 音频服务，防止 root 音频客户端改变共享目录权限。升级后需重建容器，让微信和 bot 都加载此配置。
- `visual_message.*`：OCR/YOLO 视觉读取兜底配置。
- `mcp.host` / `mcp.port`：容器内 MCP 监听地址和端口。
- `mqtt.host`：旧式/远程 MQTT 转发；留空表示关闭。

常用环境变量：

- `WECHAT_AI_DASHBOARD_USERNAME` / `WECHAT_AI_DASHBOARD_PASSWORD`：管理台 Basic Auth 账号密码。
- `WECHAT_AI_MCP_TOKEN`：MCP Bearer token，必须改掉默认值。
- `WECHAT_AI_MCP_ADMIN_ENABLED`：是否允许 MCP 执行本地运维类变更，默认 `false`。
- `WECHAT_AI_MCP_WRITE_ENABLED`：是否允许 MCP 执行真实微信写操作，默认 `false`。

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

MCP 和 bot 在同一个进程里运行时，写操作会直接进入本地 `rpa_task_queue`，不依赖 MQTT。只有显式配置 `mqtt.host` 且没有本地 bot 队列时，才会走 MQTT dispatcher。

## 开发和测试

容器内运行测试：

```bash
docker exec wechat-ai bash -lc 'cd /app && PYTHONPATH=/app/src /opt/venv-bot/bin/python -m unittest discover -s /app/tests -t /app -v'
```

常用命令：

```bash
docker exec wechat-ai /scripts/health-check.sh
docker compose logs -f wechat-ai
tail -f config/logs/bot.log
```

使用当前 `./config` 重建并重启：

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

项目集成和移植了 `omni-bot-sdk-oss` 的 bot、RPA、插件和消息解析思路及部分代码，因此遵循其 GPL-3.0-or-later 授权要求。容器桌面与 Selkies/微信封装方案参考 `wechat-selkies`，该项目采用 MIT License。

详见 [LICENSE](LICENSE) 和 [CREDITS.md](CREDITS.md)。
