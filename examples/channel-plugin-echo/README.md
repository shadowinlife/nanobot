# Webhook Channel Plugin Example

This folder is a complete channel plugin example for nanobot with real HTTP integration.

It demonstrates how to add a custom channel from an external package without modifying nanobot core code.

## What this plugin provides

- Channel factory entry point: `nanobot.channel_factories`
- Channel name: `webhook_dev` (entry point key `webhook-dev` is normalized to `webhook_dev`)
- Outbound integration: sends POST requests to your webhook endpoint
- Inbound integration (optional): polls an HTTP endpoint and pushes events into nanobot

## Install in editable mode

```bash
cd examples/channel-plugin-echo
uv pip install -e .
```

## Reload plugins in nanobot

```bash
nanobot channels reload
```

If gateway is running in the same config context, this command sends `SIGHUP` so changes apply online.

## Configure nanobot

Merge this into `~/.nanobot/config.json`:

```json
{
  "channels": {
    "plugins": {
      "webhook_dev": {
        "enabled": true,
        "outboundUrl": "https://example.com/bot/outbound",
        "inboundPollUrl": "https://example.com/bot/inbound/poll",
        "authToken": "YOUR_TOKEN",
        "pollIntervalSeconds": 2,
        "timeoutSeconds": 10,
        "defaultChatId": "demo-room",
        "allowFrom": ["*"]
      }
    }
  }
}
```

## Inbound Poll API format

Your `inboundPollUrl` should return JSON like:

```json
{
  "events": [
    {
      "sender_id": "user-001",
      "chat_id": "demo-room",
      "content": "hello from external platform",
      "metadata": {
        "source": "my-platform"
      }
    }
  ]
}
```

Top-level list is also supported.

## Verify quickly

1. Start gateway in one terminal:

```bash
nanobot gateway
```

2. Send a message to plugin channel from CLI (example prompt):

```bash
nanobot agent -m "Use message tool to send 'hello from plugin channel' to channel webhook_dev chat_id demo-room"
```

3. Check your webhook server logs, you should receive payloads like:

```json
{
  "channel": "webhook_dev",
  "chat_id": "demo-room",
  "content": "hello from plugin channel",
  "metadata": {},
  "sender_id": "nanobot"
}
```

## Factory contract

The factory signature expected by nanobot is:

```python
def create_channel(*, config, bus, channel_name, app_config):
    ...
```

- `config` comes from `channels.plugins.<channel_name>`
- `bus` is the shared runtime `MessageBus`
- `channel_name` is normalized to snake_case
- `app_config` is the root config object

## Notes

- `outboundUrl` is required.
- If `inboundPollUrl` is omitted, this channel works as outbound-only.
- For debugging, you can temporarily use `https://webhook.site/` as outbound endpoint.
