# MCP 使用指南

WeChat-AI 在 bot 同一进程内暴露 Streamable HTTP MCP Server。默认 Docker Compose 配置下：

- 宿主机 endpoint：`http://localhost:8100/mcp`
- 容器内 endpoint：`http://localhost:8000/mcp`
- 管理面板：`http://localhost:8100/dashboard`

MCP Server 能读取聊天记录并提交微信 RPA 动作，只适合作为内部服务使用。不要在没有反向代理鉴权、访问控制和审计的情况下暴露到公网。

## 推荐 Agent 工作流

1. 调用 `get_runtime_status`，确认数据库、RPA、微信窗口可用。
2. 调用 `search_contacts`，解析精确联系人或群聊。
3. 调用 `get_recent_messages`、`query_wechat_msg` 或 `get_chat_summary_context`，读取必要上下文。
4. 发消息前先调用 `send_text_msg` 并设置 `dry_run=true`。
5. 把解析到的目标和即将发送的文本展示给人类确认。
6. 人类明确确认后，再调用 `send_text_msg` 发送。

`leave_room`、`public_room_announcement` 这类群操作必须要求用户明确提出该操作，不能由 Agent 自行推断执行。

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

### 联系人和会话

| 工具 | 用途 |
| --- | --- |
| `search_contacts` | 按微信 ID、备注、昵称、alias 模糊搜索联系人/群聊 |
| `get_recent_chats` | 按最近消息时间列出有消息表的会话 |
| `query_room_member_list` | 从数据库读取群成员列表 |

### 消息读取

| 工具 | 用途 |
| --- | --- |
| `query_wechat_msg` | 按联系人、关键词、时间范围查询文本历史 |
| `get_recent_messages` | 获取最近消息，文本直接返回，媒体消息返回类型摘要 |
| `get_chat_summary_context` | 返回适合 LLM 上下文窗口的紧凑聊天行 |

### 已移植写操作

| 工具 | 用途 | 注意 |
| --- | --- | --- |
| `send_text_msg` | 文本消息入 RPA 队列 | 发送前先用 `dry_run=true` |
| `public_room_announcement` | 群公告入 RPA 队列 | 需要账号有群管理权限 |
| `leave_room` | 退群操作入 RPA 队列 | 破坏性操作，必须人工确认 |

### Linux 侧尚未移植

以下工具仍保留注册，但会返回 `status: "unavailable"`：

- `send_file_msg`
- `send_pat_msg`
- `remove_room_member`
- `invite_room_member`
- `rename_room_name`
- `rename_name_in_room`

保留这些工具是为了稳定 API 形态；在 Linux RPA handler 移植完成前，Agent 不能把它们当成成功动作。

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
    "message": "我稍后回复你。"
  }
}
```
