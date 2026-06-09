# OpenClaw 接入

使用 WeChat-AI 作为远程 MCP Server：

```text
http://localhost:8100/mcp
```

如果 OpenClaw 配置支持 MCP server JSON，可配置 Streamable HTTP：

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

如果你的 OpenClaw 版本使用 `type` 字段而不是 `transport`，保持 URL 不变，将 server 类型设为 Streamable HTTP。

推荐系统提示词：

```text
使用 wechat-ai MCP server 作为受控微信操作后端。
每次先调用 get_runtime_status。窗口尺寸异常时可以调用 reset_wechat_window。
读取或写入前必须用 search_contacts 或 get_contact_detail 解析目标。
回复前用 get_chat_summary_context、get_recent_messages 或 search_text_messages 读取必要上下文。
发送消息前调用 send_text_msg 且 dry_run=true，等待人类明确确认后再发送。
send_text_msg 返回 status="executed" 才表示执行完成；status="queued" 只表示已入队。
除非用户明确要求，不要使用群公告、退群或其他群管理工具。
```
