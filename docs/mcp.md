# MCP 使用指南

WeChat-AI 在 bot 同一进程内暴露 Streamable HTTP MCP Server。默认 Docker Compose 配置下：

- 宿主机 endpoint：`http://localhost:8100/mcp`
- 容器内 endpoint：`http://localhost:8000/mcp`
- 管理面板：`http://localhost:8100/dashboard`

MCP Server 能读取聊天记录并提交微信 RPA 动作，只适合作为内部服务使用。不要在没有反向代理鉴权、访问控制和审计的情况下暴露到公网。

默认安全策略：

- MCP endpoint 需要 `Authorization: Bearer <WECHAT_AI_MCP_TOKEN>`。
- 默认 `WECHAT_AI_MCP_TOKEN=wechat` 会被拒绝访问。
- 认证通过后，读取类 MCP 工具可用。
- `refresh_database`、`reset_wechat_window`、`set_message_polling` 默认被拦截，需要设置 `WECHAT_AI_MCP_ADMIN_ENABLED=true`。
- 发送消息、文件、拍一拍、群公告、退群、邀请/移除群成员、改群名等真实写操作默认被拦截，需要设置 `WECHAT_AI_MCP_WRITE_ENABLED=true`。
- 即使开启写操作，也建议 Agent 先用 `dry_run=true`，把目标、正文和群操作参数展示给人确认后再执行。

## 推荐 Agent 工作流

1. 调用 `get_runtime_status`，确认数据库、RPA、微信窗口可用。
2. 如果窗口状态异常，调用 `reset_wechat_window` 修正微信窗口尺寸和布局。
3. 调用 `search_contacts` 或 `get_contact_detail`，解析精确联系人或群聊。
4. 调用 `get_recent_messages`、`search_text_messages` 或 `get_chat_summary_context`，读取必要上下文。
5. 调用写操作前先设置 `dry_run=true`，只解析目标和参数。
6. 把解析到的目标、完整文本或群操作参数展示给人类确认。
7. 人类明确确认后，再调用对应写工具执行。

`leave_room`、`public_room_announcement`、`remove_room_member`、`invite_room_member`、`rename_room_name`、`rename_name_in_room` 这类群操作必须要求用户明确提出该操作，不能由 Agent 自行推断执行；确认后的真实执行调用必须传 `confirm=true`。

如果未开启对应环境变量，工具会返回 `status="blocked"`，这不是运行错误，而是安全开关生效。如果高风险群操作未传 `confirm=true`，会返回 `status="confirmation_required"`。

## 客户端接入

### 支持 Streamable HTTP 的客户端

直接配置：

```text
http://localhost:8100/mcp
```

请求头需要带：

```http
Authorization: Bearer <WECHAT_AI_MCP_TOKEN>
```

### Claude Code

Claude Code 可添加 HTTP MCP Server：

```bash
claude mcp add --transport http wechat-ai http://localhost:8100/mcp
```

推荐配套提示词见 [Claude Code 接入](clients/claude-code.md)。

### 只支持 stdio 的客户端

如果客户端只支持 stdio MCP，可以使用支持自定义 HTTP header 的 HTTP-to-stdio bridge，例如 `mcp-remote`。不同 bridge 的 header 参数不完全一致，核心要求是转发以下请求头：

```jsonc
{
  "mcpServers": {
    "wechat-ai": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://localhost:8100/mcp"
      ]
      // 需要按 bridge 文档附加请求头：
      // Authorization: Bearer ${WECHAT_AI_MCP_TOKEN}
    }
  }
}
```

本项目不内置该 bridge，请按你的客户端环境自行安装和维护。

### OpenClaw

OpenClaw 或类似支持 MCP JSON 配置的客户端可使用 Streamable HTTP：

```json
{
  "mcpServers": {
    "wechat-ai": {
      "transport": "streamable-http",
      "url": "http://localhost:8100/mcp",
      "headers": {
        "Authorization": "Bearer ${WECHAT_AI_MCP_TOKEN}"
      }
    }
  }
}
```

如果你的客户端字段名是 `type` 而不是 `transport`，保持 URL 不变，并按客户端文档选择 Streamable HTTP 类型。

## 工具列表

### 状态与身份

| 工具 | 用途 |
| --- | --- |
| `get_timestamp` | 当前 Unix 毫秒时间戳 |
| `get_runtime_status` | bot、RPA、数据库、队列、YOLO、dashboard 状态 |
| `get_wechat_user_info` | 当前微信身份信息，敏感字段脱敏 |
| `get_database_status` | 数据库发现、key、联系人、消息表状态 |
| `refresh_database` | 重新扫描数据库、key、联系人和消息表 |
| `get_wechat_window_status` | 微信窗口尺寸、布局和 YOLO/RPA 对齐状态 |
| `reset_wechat_window` | 将微信窗口恢复到 RPA/YOLO 期望尺寸 |
| `discover_dat_keys` | 自动发送图片 probe、定位 DAT、推断 XOR key 并派生/扫描 AES key |
| `set_message_polling` | 暂停或恢复数据库消息监听 |

### 联系人和会话

| 工具 | 用途 |
| --- | --- |
| `search_contacts` | 按微信 ID、备注、昵称、alias 模糊搜索联系人/群聊 |
| `get_contact_detail` | 解析单个联系人/群聊并返回详细状态 |
| `get_recent_chats` | 按最近消息时间列出有消息表的会话 |
| `query_room_member_list` | 从数据库读取群成员列表 |

### 消息读取

| 工具 | 用途 |
| --- | --- |
| `query_wechat_msg` | 按联系人、关键词、时间范围查询文本历史 |
| `search_text_messages` | 结构化查询文本历史，返回联系人、时间和消息列表 |
| `get_recent_messages` | 获取最近消息；`parse_media=true` 时尝试解析媒体路径 |
| `get_recent_media_messages` | 获取最近图片、视频、文件等非文本消息，并尽量返回本地路径 |
| `get_chat_summary_context` | 返回适合 LLM 上下文窗口的紧凑聊天行 |

### 已移植写操作

| 工具 | 用途 | 注意 |
| --- | --- | --- |
| `send_text_msg` | 文本消息入 RPA 队列并可等待执行结果 | 发送前先用 `dry_run=true` |
| `send_file_msg` | 发送容器内可访问的本地文件 | `file_path` 必须是容器内路径 |
| `send_pat_msg` | 对联系人或群成员执行“拍一拍” | 群内拍一拍要求目标头像在当前消息区可见 |
| `public_room_announcement` | 群公告入 RPA 队列 | 需要账号有群管理权限，真实执行需 `confirm=true` |
| `leave_room` | 退群操作入 RPA 队列 | 破坏性操作，真实执行需 `confirm=true` |
| `remove_room_member` | 移除群成员 | 破坏性操作，真实执行需 `confirm=true` |
| `invite_room_member` | 邀请联系人进群 | 需要当前账号有权限，真实执行需 `confirm=true` |
| `rename_room_name` | 修改群名 | 高影响操作，真实执行需 `confirm=true` |
| `rename_name_in_room` | 修改自己在群内的昵称 | 高影响操作，真实执行需 `confirm=true` |

这些写操作都依赖当前微信界面、窗口尺寸、OCR 和 YOLO 识别结果。调用前建议先确认 `get_wechat_window_status` 返回窗口已对齐；异常时先调用 `reset_wechat_window`。

## 参数协议

MCP 工具参数一律使用 `snake_case`，不要把 Web API、RPA action 或自然语言里的字段名混用进 MCP 调用。写操作前建议先调用 `dry_run=true`，根据返回的 `resolved`、`action_data` 和 `message_preview` 向人类展示确认内容。

不要向 MCP 写工具传 `target`、`content`、`is_chatroom`、`at_list` 这类内部字段；这些字段属于 RPA payload，不是 MCP 协议。MCP 会根据 `recipient_name` / `room_name` 解析联系人并自动判断是否群聊。

### 通用约定

| 字段 | 适用工具 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `dry_run` | 写操作 | `false` | `true` 时只解析目标和动作参数，不进入 RPA 队列，也不要求写开关。 |
| `wait_seconds` | RPA 写操作 | 文本 `12`，群改名类 `25`，其他 `12` | 等待本地 RPA 执行结果；`0` 表示只入队不等待。文本发送范围 `0-30` 秒，其他 RPA 动作范围 `0-60` 秒。 |
| `confirm` | 高影响群操作 | `false` | 退群、群公告、邀请/移除成员、改群名、改群昵称真实执行时必须传 `true`。 |
| `contact_name` / `recipient_name` / `room_name` | 联系人、消息和写操作 | 无 | 可以是微信 ID、群 ID、备注、昵称或显示名；Agent 应先用 `search_contacts` / `get_contact_detail` 解析。 |

### 状态与运维工具

| 工具 | 参数 |
| --- | --- |
| `get_timestamp` | 无 |
| `get_runtime_status` | 无 |
| `get_wechat_user_info` | 无 |
| `get_wechat_window_status` | 无 |
| `reset_wechat_window` | 无。需要 `WECHAT_AI_MCP_ADMIN_ENABLED=true`。 |
| `get_database_status` | 无 |
| `refresh_database` | 无。需要 `WECHAT_AI_MCP_ADMIN_ENABLED=true`。 |
| `set_message_polling` | `paused: bool`。`true` 暂停消息监听，`false` 恢复。需要 `WECHAT_AI_MCP_ADMIN_ENABLED=true`。 |
| `discover_dat_keys` | `send_probe: bool = true`，`scan_timeout_seconds: float = 120`。需要 admin；`send_probe=true` 时还需要 write。 |

### 联系人与消息读取工具

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `search_contacts` | `query: str`，`limit: int = 20`，`include_chatrooms: bool = true`，`include_contacts: bool = true` | `limit` 范围 `1-100`。 |
| `get_contact_detail` | `contact_name: str` | 返回联系人/群聊详情；群聊会尽量返回成员数量。 |
| `get_recent_chats` | `limit: int = 20` | `limit` 范围 `1-100`。 |
| `query_room_member_list` | `room_name: str` | 从数据库读取群成员列表，常用于群聊 @、拍一拍、移除成员前确认成员名。 |
| `get_recent_messages` | `contact_name: str`，`limit: int = 30`，`include_non_text: bool = true`，`parse_media: bool = false` | `limit` 范围 `1-200`；`parse_media=true` 时解析图片、视频、文件路径。 |
| `search_text_messages` | `contact_name: str`，`query: str \| null = null`，`start_timestamp: int \| null = null`，`end_timestamp: int \| null = null`，`limit: int = 100` | 时间戳为 Unix 秒；`limit` 范围 `1-500`。 |
| `get_recent_media_messages` | `contact_name: str`，`limit: int = 20` | 只返回最近非文本消息，`limit` 范围 `1-100`。 |
| `get_chat_summary_context` | `contact_name: str`，`limit: int = 40` | 返回适合放进 LLM 上下文的紧凑文本，`limit` 范围 `1-120`。 |
| `query_wechat_msg` | `contact_name: str`，`query: str \| null = null`，`start_timestamp: int \| null = null`，`end_timestamp: int \| null = null`，`limit: int = 500` | 兼容旧版文本查询工具，新接入优先用 `search_text_messages`。 |

### 写操作工具

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `send_text_msg` | `recipient_name: str`，`message: str`，`at_user_name: str \| null = null`，`dry_run: bool = false`，`wait_seconds: float = 12` | 给联系人或群聊发文本。群聊 @ 使用 `at_user_name`，不要把 `@成员名` 手写进 `message`。 |
| `send_file_msg` | `recipient_name: str`，`file_path: str`，`dry_run: bool = false`，`wait_seconds: float = 12` | `file_path` 必须是容器内可读文件路径，例如 `/config/exports/report.pdf`。 |
| `send_pat_msg` | `user_name: str`，`room_name: str \| null = null`，`dry_run: bool = false`，`wait_seconds: float = 12` | 私聊拍一拍只传 `user_name`；群聊拍一拍传 `room_name` 和群内成员 `user_name`。 |
| `public_room_announcement` | `room_name: str`，`content: str`，`force_edit: bool = false`，`confirm: bool = false` | 发布或编辑群公告；真实执行必须 `confirm=true`。 |
| `leave_room` | `room_name: str`，`confirm: bool = false` | 退群；真实执行必须 `confirm=true`。 |
| `remove_room_member` | `room_name: str`，`member_name: str`，`dry_run: bool = false`，`wait_seconds: float = 12`，`confirm: bool = false` | 移除群成员；真实执行必须 `confirm=true`。 |
| `invite_room_member` | `room_name: str`，`user_name: str`，`dry_run: bool = false`，`wait_seconds: float = 12`，`confirm: bool = false` | 邀请联系人进群；真实执行必须 `confirm=true`。 |
| `rename_room_name` | `room_name: str`，`new_name: str`，`dry_run: bool = false`，`wait_seconds: float = 25`，`confirm: bool = false` | 修改群名；真实执行必须 `confirm=true`。 |
| `rename_name_in_room` | `room_name: str`，`new_name_in_room: str`，`dry_run: bool = false`，`wait_seconds: float = 25`，`confirm: bool = false` | 修改当前账号在群里的昵称；真实执行必须 `confirm=true`。 |

### 群聊 @ 协议

群聊 @ 必须通过 `send_text_msg.at_user_name` 表达，`message` 只放真正要发送的正文。比如用户要求“在测试群 @Bread 发送 机器人测试”，调用应写成：

```json
{
  "tool": "send_text_msg",
  "arguments": {
    "recipient_name": "测试群",
    "message": "机器人测试",
    "at_user_name": "Bread",
    "dry_run": true
  }
}
```

确认后再去掉 `dry_run`：

```json
{
  "tool": "send_text_msg",
  "arguments": {
    "recipient_name": "测试群",
    "message": "机器人测试",
    "at_user_name": "Bread",
    "wait_seconds": 20
  }
}
```

注意：

- 不要把正文写成 `@Bread 机器人测试`，否则 RPA 会把 `@` 当作普通输入或造成重复 @。
- `at_user_name` 传群成员在微信里可搜索/可显示的名字，例如备注、群昵称或昵称。成员名不确定时，先调用 `query_room_member_list`。
- 当前 MCP 协议一次只接受一个 `at_user_name`；需要多人 @ 时，应先确认前端 RPA 支持情况，避免自行拼接多个 `@`。

### 返回状态

工具返回 JSON 字符串。Agent 不应只看 HTTP 成功，而要检查 JSON 里的 `status`：

| `status` | 含义 | Agent 处理方式 |
| --- | --- | --- |
| `ok` | 读取或运维工具成功。 | 可以使用返回数据继续任务。 |
| `dry_run` | 写操作只完成目标解析，没有执行。 | 展示 `resolved`、`action_data`、`message_preview` 给人确认。 |
| `queued` | 动作已进入 RPA 队列，但没有确认执行完成。 | 不要直接宣称成功；可查看日志、状态或等待用户确认。 |
| `executed` | RPA 返回完成，并且 `result.ok=true`。 | 可以说明操作已执行。 |
| `failed` | RPA 返回完成，但执行失败。 | 把 `result.reason` / `result.error` 展示给用户，不要自动重复高风险操作。 |
| `timeout` | 动作已提交，但等待超时。 | 不要盲目重试发送，先检查微信界面、队列和日志，避免重复操作。 |
| `blocked` | MCP 安全开关未开启。 | 提示用户检查 `WECHAT_AI_MCP_ADMIN_ENABLED` 或 `WECHAT_AI_MCP_WRITE_ENABLED`。 |
| `confirmation_required` | 高影响操作缺少 `confirm=true`。 | 先向人类确认，确认后再带 `confirm=true` 调用。 |
| `invalid_request` | 参数缺失或不合法。 | 修正参数名、路径、目标名后重试。 |
| `not_found` | 未找到联系人、群聊或成员。 | 先用搜索工具重新解析目标。 |
| `unavailable` / `error` | 运行时、数据库、窗口或工具异常。 | 停止写操作，报告阻塞原因。 |

## Dashboard 与 MCP 暴露建议

Dashboard 地址是 `http://localhost:8100/dashboard`，内置 Basic Auth。账号密码来自 `WECHAT_AI_DASHBOARD_USERNAME` / `WECHAT_AI_DASHBOARD_PASSWORD`，默认 `wechat/wechat` 会被拒绝登录。Dashboard 状态变更 POST 还要求 `X-WeChat-AI-Dashboard: 1`，页面会自动携带；脚本调用时需要手动加。MCP 地址是 `http://localhost:8100/mcp`，需要 Bearer token，默认 `WECHAT_AI_MCP_TOKEN=wechat` 会被拒绝。

MCP 本身面向可信 Agent 客户端。把 `8100` 端口暴露到其他机器前，请至少满足以下条件：

- 使用 HTTPS 反向代理或 VPN，不裸露明文公网访问。
- 反向代理层增加登录鉴权、IP 白名单或内网访问控制。
- 只在确实需要时开启 `WECHAT_AI_MCP_ADMIN_ENABLED` 和 `WECHAT_AI_MCP_WRITE_ENABLED`。
- 不把 `.env`、`config/`、`xwechat_files/` 和容器内 `/config` 目录同步到不可信位置。

## 媒体与图片 DAT

`get_recent_messages(parse_media=true)` 和 `get_recent_media_messages` 会尽量返回图片、视频、文件的本地路径。Dashboard 的 `/dashboard/media?path=...` 只代理允许目录内的本地文件。

Linux 微信 4.x 图片通常是加密 `.dat`：

- 已配置 `aes_xor_key` 时，Dashboard 会尝试解密后以内联 PNG/JPEG/GIF 返回。
- 未配置 DAT AES key 时，媒体接口返回 JSON 诊断，包含 DAT 版本、分段大小、是否缺 key 等信息；这表示路径解析成功，但图片内容还不能显示。
- `aes_xor_key` 写在 `config.yaml` 顶层，格式为 `AES文本key,60` 或 `hex:<hexkey>,60`。
- Dashboard 顶部“发现图片密钥”和 MCP `discover_dat_keys` 会自动生成 probe 图片并发送到文件传输助手，然后定位 `_h.dat`、推断 XOR key，并优先用 Linux 微信的 `kvcomm/key_<code>_*.statistic` 与账号目录派生 AES key；派生失败时再扫描 WeChat 进程内存候选。该工具需要 `WECHAT_AI_MCP_ADMIN_ENABLED=true`；如果 `send_probe=true`，还需要 `WECHAT_AI_MCP_WRITE_ENABLED=true`。
- 发现成功后会写回 `config.yaml` 顶层 `aes_xor_key`。后续 Dashboard 正常复用持久化 key；如果媒体解密失败，会先基于当前 DAT 和本地 `kvcomm` 做一次不发送消息的轻量刷新，仍失败时才需要重新触发完整 probe 流程。

## 示例

搜索联系人：

```json
{
  "tool": "search_contacts",
  "arguments": {
    "query": "张三",
    "limit": 5
  }
}
```

准备发送但不真正发送：

```json
{
  "tool": "send_text_msg",
  "arguments": {
    "recipient_name": "张三",
    "message": "我稍后回复你。",
    "dry_run": true
  }
}
```

人工确认后发送：

```json
{
  "tool": "send_text_msg",
  "arguments": {
    "recipient_name": "张三",
    "message": "我稍后回复你。",
    "wait_seconds": 12
  }
}
```

群聊 @：

```json
{
  "tool": "send_text_msg",
  "arguments": {
    "recipient_name": "测试群",
    "message": "机器人测试",
    "at_user_name": "Bread",
    "dry_run": true
  }
}
```

重置微信窗口尺寸：

```json
{
  "tool": "reset_wechat_window",
  "arguments": {}
}
```

查询媒体消息：

```json
{
  "tool": "get_recent_media_messages",
  "arguments": {
    "contact_name": "文件传输助手",
    "limit": 10
  }
}
```
