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
每次先调用 get_runtime_status。读取或写入前必须用 search_contacts 解析目标。
发送消息前调用 send_text_msg 且 dry_run=true，等待人类明确确认后再发送。
除非用户明确要求，不要使用群公告、退群或其他群管理工具。
```
