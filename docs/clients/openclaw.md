# OpenClaw 接入

使用 WeChat-AI 作为远程 MCP Server：

```text
http://localhost:8100/mcp
```

请求头需要带：

```http
Authorization: Bearer <WECHAT_AI_MCP_TOKEN>
```

如果 OpenClaw 配置支持 MCP server JSON，可配置 Streamable HTTP：

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

如果你的 OpenClaw 版本使用 `type` 字段而不是 `transport`，保持 URL 不变，将 server 类型设为 Streamable HTTP。

推荐系统提示词：

```text
使用 wechat-ai MCP server 作为受控微信操作后端。
每次先调用 get_runtime_status。窗口尺寸异常时可以调用 reset_wechat_window。
读取或写入前必须用 search_contacts 或 get_contact_detail 解析目标。
回复前用 get_chat_summary_context、get_recent_messages 或 search_text_messages 读取必要上下文。
调用任何写工具前先设置 dry_run=true，等待人类明确确认后再执行。
高风险群操作必须在确认后的执行调用中传 confirm=true。
写工具返回 status="executed" 才表示执行完成；status="queued" 只表示已入队。
status="blocked" 表示服务端安全开关未开启；status="confirmation_required" 表示缺少 confirm=true。
除非用户明确要求，不要使用群公告、退群、群成员或群名管理工具。
```

默认 `WECHAT_AI_MCP_TOKEN=wechat` 会被拒绝；认证通过后只开放读取类 MCP 工具。运行时管理操作需要 `WECHAT_AI_MCP_ADMIN_ENABLED=true`，真实微信写操作需要 `WECHAT_AI_MCP_WRITE_ENABLED=true`。
