# WeChat-AI

WeChat-AI is a containerized Linux WeChat automation runtime with Selkies
WebRTC, SQLCipher message access, visual RPA, plugin hooks, and a Streamable HTTP
MCP server for AI clients.

The main documentation is Chinese-first because this project targets WeChat
users and Chinese deployment contexts. See [README.md](README.md) for the full
guide.

## Quick Start

```bash
docker compose up -d --build
```

Default host URLs:

| Service | URL |
| --- | --- |
| Selkies desktop | `https://localhost:3101` |
| Dashboard | `http://localhost:8100/dashboard` |
| MCP Streamable HTTP | `http://localhost:8100/mcp` |

Recommended MCP flow:

1. `get_runtime_status`
2. `search_contacts`
3. `get_recent_messages` or `get_chat_summary_context`
4. The write tool you need with `dry_run=true`
5. Human confirmation
6. The same write tool without `dry_run`

Ported write tools include `send_text_msg`, `send_file_msg`, `send_pat_msg`,
`public_room_announcement`, `leave_room`, `remove_room_member`,
`invite_room_member`, `rename_room_name`, and `rename_name_in_room`.
Group-management operations require explicit human confirmation.

See [docs/mcp.md](docs/mcp.md). The MCP docs are Chinese-first but include the
client configuration snippets needed for Claude Code and OpenClaw.
