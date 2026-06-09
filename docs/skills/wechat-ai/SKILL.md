# WeChat-AI MCP Skill

当用户要求你通过 `wechat-ai` MCP server 检查或操作微信时，使用本 skill。

## 能力

- 使用 `get_runtime_status` 检查运行状态。
- 使用 `get_database_status`、`get_wechat_window_status`、`reset_wechat_window` 做必要的运行时排障。
- 使用 `search_contacts` 或 `get_contact_detail` 搜索联系人和群聊。
- 使用 `get_recent_messages`、`search_text_messages`、`get_recent_media_messages` 或 `get_chat_summary_context` 读取上下文。
- 在 dry run 和人工确认后，使用 `send_text_msg`、`send_file_msg` 或明确要求的群操作工具执行写操作，并根据返回状态判断是否执行完成。

## 工作流

1. 调用 `get_runtime_status`。
2. 如果数据库、RPA 或微信窗口不可用，停止；窗口尺寸异常时可以调用 `reset_wechat_window`。
3. 使用 `search_contacts` 或 `get_contact_detail` 解析目标。
4. 发送前读取上下文。
5. 调用对应写工具，设置 `dry_run=true`。
6. 请用户确认解析到的目标、完整消息或群操作参数。
7. 只有确认后，才调用不带 `dry_run` 的写工具。

## 安全规则

- 写工具返回 `status: "executed"` 才表示已执行完成。
- `status: "queued"` 只表示消息已入队；`status: "timeout"` 表示无法确认执行结果。
- `status: "unavailable"` 或 `status: "failed"` 是硬失败。
- 除非用户明确要求，不调用 `leave_room`、`public_room_announcement`、`remove_room_member`、`invite_room_member`、`rename_room_name` 或 `rename_name_in_room`。
