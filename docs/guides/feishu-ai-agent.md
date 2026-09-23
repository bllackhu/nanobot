# Build a Feishu AI Agent with nanobot

This guide connects nanobot to Feishu or Lark through the `feishu` channel. The
channel uses a WebSocket long connection, so the first setup does not require a
public webhook URL.

## What this guide builds

- a Feishu/Lark bot app connected to nanobot
- the `feishu` channel enabled in `config.json`
- one pairing-approved Feishu or Lark user
- mention-only group behavior for first deployment

## Prerequisites

- A working local nanobot reply:

```bash
nanobot agent -m "Hello!"
```

- A Feishu or Lark account that can create or approve bot apps.
- Permission to run `nanobot gateway` continuously.

## Install nanobot

```bash
python -m pip install nanobot-ai
nanobot onboard --wizard
```

## Enable the Feishu channel

Install the optional channel dependency:

```bash
nanobot plugins enable feishu
```

The easiest path is QR login:

```bash
nanobot channels login feishu
```

Open the printed URL or scan the QR code. nanobot writes the generated `appId`,
`appSecret`, `domain`, and `enabled` fields into the active config.

If QR login is unavailable, create a Feishu/Lark app manually and merge this
shape into `~/.nanobot/config.json`:

```json
{
  "channels": {
    "feishu": {
      "enabled": true,
      "appId": "cli_xxx",
      "appSecret": "xxx",
      "groupPolicy": "mention",
      "streaming": true,
      "domain": "feishu"
    }
  }
}
```

Omitting `allowFrom` enables pairing-only mode. A new user should DM the bot,
get a pairing code, and be approved before using the bot normally.

For manual apps, enable the Bot capability, receive-message events, and Long
Connection mode. If your app cannot get the `cardkit:card:write` permission,
set `"streaming": false`.

When `groupPolicy` is `"listen"`, also subscribe to `im.message.recalled_v1`
(撤回消息). Unmentioned group messages that have not yet been included in an
@mention LLM turn are then dropped from session history if the user recalls
them. Edits are not applied.

## Run nanobot gateway

```bash
nanobot channels status
nanobot gateway
```

## Test a message

DM the bot first. It should return a pairing code. Approve it from a trusted
local surface:

```bash
nanobot agent -m "/pairing approve ABCD-EFGH"
```

After approval, DM the bot again or mention it in a group chat:

```text
@nanobot Hello from Feishu
```

## Security notes

- Prefer pairing-only mode for first setup. Add `allowFrom` only when you want a
  static allowlist.
- Keep `groupPolicy` as `"mention"` before inviting the bot into busy groups.
  Use `"listen"` when you want the bot to silently follow group context and
  only reply when @mentioned. Recalled unmentioned messages are removed from
  that context if they have not yet reached an LLM turn (`im.message.recalled_v1`).
  Use `"open"` only when every group message
  should trigger a reply.
- Store app secrets through environment variables for deployed services.
- Review file, shell, and web tool access before adding more users.

## Troubleshooting

- If QR login is unavailable, use manual app setup from the full chat-apps
  reference.
- If streaming cards fail, confirm `cardkit:card:write` or set
  `"streaming": false`.
- If no messages arrive, check Feishu/Lark event permissions, Long Connection
  mode, and `nanobot gateway --verbose`.
- If a first DM returns a pairing code, approve it before testing normal
  replies.
- If DM replies fail with `code=230101` (`Sending messages to users is
  temporarily unavailable`), the bot is trying to send to the sender's
  `open_id` (`ou_...`). Feishu rejects that for these apps; replies must target
  the direct-message conversation's `chat_id` (`oc_...`). Upgrade nanobot to a
  build that delivers p2p replies to the conversation `chat_id` (the sender
  `open_id` is still used for authorization and pairing). As an interim
  workaround, set `"replyToMessage": true` so text replies use the Reply API.

## QR login scopes and events

QR scan-to-create login pre-fills the app-confirmation page with the scopes and
events the bot needs. By default it requests `cardkit:card:write` (CardKit
streaming replies) and `im.message.recalled_v1` (listen-mode recall). You can
override these per channel under `channels.feishu`:

```json
{
  "channels": {
    "feishu": {
      "qrLoginScopes": ["cardkit:card:write"],
      "qrLoginEvents": ["im.message.recalled_v1"]
    }
  }
}
```

Semantics:

- Omit both (or set to `null`) to use the built-in defaults above.
- Set a category to `[]` to drop it (no scopes / no events requested).
- Set a category to a non-empty list to replace the default for that category.

Both snake_case (`qr_login_scopes`) and camelCase (`qrLoginScopes`) keys are
accepted.

## Next: memory, automations, MCP tools

- [Chat Apps reference](../chat-apps.md)
- [Pairing](../configuration.md#pairing)
- [AI Agent Memory](./ai-agent-memory.md)
- [Configure MCP tools](./configure-mcp-tools.md)
