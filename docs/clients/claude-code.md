# Claude Code 接入

WeChat-AI 的 MCP endpoint 是 Streamable HTTP：

```text
http://localhost:8100/mcp
```

请求头需要带：

```http
Authorization: Bearer <WECHAT_AI_MCP_TOKEN>
```

添加到 Claude Code 时，需要使用客户端支持的方式配置 Authorization header：

```bash
claude mcp add --transport http wechat-ai http://localhost:8100/mcp
```

推荐项目/会话提示词：

```text
你可以通过 wechat-ai MCP server 操作一个私有微信运行时。

规则：
- 每次会话先调用 get_runtime_status。数据库、RPA 或微信窗口不可用时停止；窗口尺寸异常时可以调用 reset_wechat_window。
- 读取或写入前必须用 search_contacts 或 get_contact_detail 解析联系人或群聊。
- 回复前用 get_chat_summary_context、get_recent_messages 或 search_text_messages 读取必要上下文。
- 调用任何写工具前必须先设置 dry_run=true。
- 向我展示解析到的目标、完整待发送文本或群操作参数，等我明确确认后才能真正执行。
- 高风险群操作必须在确认后的执行调用中传 confirm=true。
- MCP 参数必须使用 snake_case。发送文本用 send_text_msg(recipient_name, message, at_user_name?, dry_run?, wait_seconds?)。
- 群聊 @ 必须传 at_user_name；message 只放正文，不要把 @成员名 手写进 message。成员名不确定时先调用 query_room_member_list。
- 发送文件用 send_file_msg(recipient_name, file_path, dry_run?, wait_seconds?)，file_path 必须是容器内可读路径。
- 拍一拍用 send_pat_msg(user_name, room_name?, dry_run?, wait_seconds?)，群聊内拍一拍要同时传 room_name 和 user_name。
- 除非我明确要求，不要调用 leave_room、public_room_announcement、remove_room_member、invite_room_member、rename_room_name 或 rename_name_in_room。
- 遇到 status="blocked" 表示服务端安全开关未开启，不要重试执行。
- 遇到 status="confirmation_required" 表示缺少 confirm=true，必须先取得明确确认。
- 写工具返回 status="executed" 才表示执行完成；status="queued" 只表示已入队；status="timeout" 表示无法确认结果。
- 遇到 status="unavailable" 或 status="failed" 必须视为失败，不能说操作成功。
```

Claude Desktop 或其他只支持 stdio MCP 的客户端，可以通过 `mcp-remote` 这类 bridge 转接：

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

MCP 服务可以读取聊天记录并提交 RPA 操作，只应放在本机或可信内网。默认 `WECHAT_AI_MCP_TOKEN=wechat` 会被拒绝；认证通过后读取工具可用，admin 和真实写操作需要分别开启 `WECHAT_AI_MCP_ADMIN_ENABLED=true`、`WECHAT_AI_MCP_WRITE_ENABLED=true`。
