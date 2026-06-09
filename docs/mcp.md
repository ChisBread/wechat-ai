# MCP 使用指南

WeChat-AI 在 bot 同一进程内暴露 Streamable HTTP MCP Server。默认 Docker Compose 配置下：

- 宿主机 endpoint：`http://localhost:8100/mcp`
- 容器内 endpoint：`http://localhost:8000/mcp`
- 管理面板：`http://localhost:8100/dashboard`

MCP Server 能读取聊天记录并提交微信 RPA 动作，只适合作为内部服务使用。不要在没有反向代理鉴权、访问控制和审计的情况下暴露到公网。

## 推荐 Agent 工作流

1. 调用 `get_runtime_status`，确认数据库、RPA、微信窗口可用。
2. 如果窗口状态异常，调用 `reset_wechat_window` 修正微信窗口尺寸和布局。
3. 调用 `search_contacts` 或 `get_contact_detail`，解析精确联系人或群聊。
4. 调用 `get_recent_messages`、`search_text_messages` 或 `get_chat_summary_context`，读取必要上下文。
5. 调用写操作前先设置 `dry_run=true`，只解析目标和参数。
6. 把解析到的目标、完整文本或群操作参数展示给人类确认。
7. 人类明确确认后，再调用对应写工具执行。

`leave_room`、`public_room_announcement`、`remove_room_member`、`invite_room_member`、`rename_room_name`、`rename_name_in_room` 这类群操作必须要求用户明确提出该操作，不能由 Agent 自行推断执行。

## 客户端接入

### 支持 Streamable HTTP 的客户端

直接配置：

```text
http://localhost:8100/mcp
```

### Claude Code

Claude Code 可添加 HTTP MCP Server：

```bash
claude mcp add --transport http wechat-ai http://localhost:8100/mcp
```

推荐配套提示词见 [Claude Code 接入](clients/claude-code.md)。

### 只支持 stdio 的客户端

如果客户端只支持 stdio MCP，可以使用 HTTP-to-stdio bridge，例如 `mcp-remote`：

```json
{
  "mcpServers": {
    "wechat-ai": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8100/mcp"]
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
      "url": "http://localhost:8100/mcp"
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
| `public_room_announcement` | 群公告入 RPA 队列 | 需要账号有群管理权限 |
| `leave_room` | 退群操作入 RPA 队列 | 破坏性操作，必须人工确认 |
| `remove_room_member` | 移除群成员 | 破坏性操作，必须人工确认 |
| `invite_room_member` | 邀请联系人进群 | 需要当前账号有权限 |
| `rename_room_name` | 修改群名 | 高影响操作，必须人工确认 |
| `rename_name_in_room` | 修改自己在群内的昵称 | 高影响操作，必须人工确认 |

这些写操作都依赖当前微信界面、窗口尺寸、OCR 和 YOLO 识别结果。调用前建议先确认 `get_wechat_window_status` 返回窗口已对齐；异常时先调用 `reset_wechat_window`。

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
