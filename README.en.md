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

Set Dashboard credentials before opening it:

```dotenv
WECHAT_AI_DASHBOARD_USERNAME=your-user
WECHAT_AI_DASHBOARD_PASSWORD=use-a-strong-password
WECHAT_AI_MCP_TOKEN=use-another-long-random-token
```

The placeholders `wechat/wechat` and `WECHAT_AI_MCP_TOKEN=wechat` are rejected
by the app. MCP requires `Authorization: Bearer <WECHAT_AI_MCP_TOKEN>`.
Authenticated read tools work by default; runtime-admin tools require
`WECHAT_AI_MCP_ADMIN_ENABLED=true`, and real WeChat write tools require
`WECHAT_AI_MCP_WRITE_ENABLED=true`.

Recommended MCP flow:

1. `get_runtime_status`
2. `search_contacts`
3. `get_recent_messages` or `get_chat_summary_context`
4. The write tool you need with `dry_run=true`
5. Human confirmation
6. The same write tool without `dry_run`

Ported write tools include `send_text_msg`, `send_file_msg`, `send_image_msg`, `send_voice_msg`, `send_pat_msg`,
`public_room_announcement`, `leave_room`, `remove_room_member`,
`invite_room_member`, `rename_room_name`, and `rename_name_in_room`.
Group-management operations require explicit human confirmation.

Contact and group metadata refresh automatically with the periodic message-map
refresh (30 seconds by default). Missing MCP contacts trigger a refresh and retry,
with metadata reloads throttled to once every 10 seconds; an explicit forced
refresh bypasses this limit. This reuses open databases without rescanning keys
or resetting message sequences. Text, file, and image sends return `not_found`
for unresolved contacts without queueing an action.

`send_image_msg` accepts image base64 or an image data URI in one MCP call, stages
a temporary file, and sends it as an image through the existing local RPA queue.
PNG/JPEG/WebP/GIF are supported up to 10 MiB decoded; `dry_run=true` validates
without writing a file or sending. See the [MCP parameter guide](docs/mcp.md#图片上传与发送)
for examples, MIME correction, and completion/timeout semantics.

Media notes:

- `send_voice_msg` sends native voice bubbles from a local audio file or base64
  using FFmpeg and an isolated PulseAudio microphone. Inputs are limited to
  10 MiB and 1–55 seconds. Requires Linux WeChat 4.1.13's light theme at 100%
  DPI. Playback runs in real time; query the original action after a timeout.
  Verification checks acknowledged voice metadata and duration, not audio
  content. Rebuild the container to apply the shared PulseAudio socket setup.

- The database reader resolves local paths for images, videos, and files.
- Linux WeChat 4.x image `.dat` files are detected and can be decrypted when
  `aes_xor_key` is configured as `AES-text-key,60` or `hex:<aes-key-hex>,60`.
- DAT V2 is parsed as a PKCS7-padded AES-ECB prefix, an optional raw middle
  segment, and an XOR tail. Common image formats are proxied directly; WeChat
  `wxgf` payloads are returned as `video/hevc` / `.hevc`.
- Without the image DAT AES key, Dashboard still shows message metadata and
  local paths, but `/dashboard/media` returns a JSON diagnostic instead of a
  broken image.
- The Dashboard button "发现图片密钥" and MCP tool `discover_dat_keys` automate
  the probe-image flow: send a probe to File Transfer Assistant, locate the new
  DAT, infer the XOR key, and first derive the AES key from Linux WeChat
  `kvcomm/key_<code>_*.statistic` plus the account directory. If that fails,
  the tool falls back to bounded WeChat memory scanning.
- A successful discovery persists `aes_xor_key` in `config.yaml`. Dashboard
  normally reuses that cached key; if media decrypt fails later, it performs a
  local no-send `kvcomm` refresh before asking you to run a full probe again.
- TODO: WeChat does not always download historical media until the GUI browses
  or opens that message. The next media milestone is a controlled GUI download
  queue that navigates to a message, triggers image/video/file download, waits
  for local files, then retries the existing media parser.

See [docs/mcp.md](docs/mcp.md). The MCP docs are Chinese-first but include the
client configuration snippets needed for Claude Code and OpenClaw.
