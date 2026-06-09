# Credits and Third-Party Notices

WeChat-AI is an integration and Linux adaptation project. It exists because
several upstream open source projects already solved hard parts of the problem.
We keep this file to make those relationships explicit.

## Primary Upstream Projects

### omni-bot-sdk-oss

- Repository: https://github.com/weixin-omni/omni-bot-sdk-oss
- Author metadata: huchundong <gycm520@gmail.com>
- License: GPL-3.0-or-later
- Local reference copy: `example/omni-bot-sdk-oss/`

This project adapts and ports ideas and code structure from omni-bot-sdk-oss,
including the bot service layout, RPA action model, plugin flow, MCP/MQTT command
shape, WeChat message models/factories, and the database-backed message pipeline.
Because of this, WeChat-AI is licensed as GPL-3.0-or-later.

### wechat-selkies

- Repository: https://github.com/nickrunning/wechat-selkies
- Copyright: Copyright (c) 2025 Nick007
- License: MIT License
- Local reference copy: `example/wechat-selkies/`

This project uses wechat-selkies as the main reference for the Selkies/Openbox
Linux desktop container shape, Linux WeChat packaging, startup scripts, and
runtime layout. The original MIT license notice is retained in the local
reference copy and acknowledged here.

## Other Important References

The upstream omni-bot-sdk-oss README credits several WeChat database and media
research projects. We also acknowledge them as important technical references
for this integration work:

- DbkeyHook: https://github.com/gzygood/DbkeyHook
- wechat-dump-rs: https://github.com/0xlane/wechat-dump-rs
- WeChatMsg: https://github.com/LC044/WeChatMsg
- wechat-dump: https://github.com/ppwwyyxx/wechat-dump
- WxDatDecrypt: https://github.com/recarto404/WxDatDecrypt

## Runtime Dependencies

The Docker image is based on LinuxServer.io Selkies base images:

- LinuxServer.io baseimage-selkies: https://github.com/linuxserver/docker-baseimage-selkies
- Selkies project: https://github.com/selkies-project

These are runtime/base-image dependencies and retain their own upstream
licenses.

## Trademark and Product Notice

WeChat is a Tencent product and trademark. This project is an independent
third-party open source integration for learning and personal automation
research. It is not affiliated with, endorsed by, or sponsored by Tencent.
