# WeChat-AI MCP Skill

当用户要求你通过 `wechat-ai` MCP server 检查或操作微信时，使用本 skill。

## 能力

- 使用 `get_runtime_status` 检查运行状态。
- 使用 `search_contacts` 搜索联系人和群聊。
- 使用 `get_recent_messages`、`query_wechat_msg` 或 `get_chat_summary_context` 读取上下文。
- 在 dry run 和人工确认后，使用 `send_text_msg` 将文本消息加入 RPA 队列。

## 工作流

1. 调用 `get_runtime_status`。
2. 如果数据库、RPA 或微信窗口不可用，停止。
3. 使用 `search_contacts` 解析目标。
4. 发送前读取上下文。
5. 调用 `send_text_msg`，设置 `dry_run=true`。
6. 请用户确认解析到的目标和完整消息。
7. 只有确认后，才调用不带 `dry_run` 的 `send_text_msg`。

## 安全规则

- 只有工具返回 `status: "queued"` 时，才说消息已进入发送队列。
- `status: "unavailable"` 是硬失败。
- 除非用户明确要求，不调用 `leave_room` 或 `public_room_announcement`。
- 不把尚未移植的工具当成 fallback。
