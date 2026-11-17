# Telegram

Connecting Telegram lets people chat with an agent in a private conversation or a group, and lets agents post messages, photos, and polls back. It is the quickest channel to set up: a Telegram bot is created in a few messages with `@BotFather`.

Read [Channels and bots](index.md) first for the concepts shared with the other platforms.

## Before you start

- You need a Telegram account.
- You need administrator rights in XTM One.
- The platform must be reachable from the internet over HTTPS, because Telegram posts messages to it.

## Step 1: Create the bot

1. Open Telegram and start a conversation with `@BotFather`.
2. Send `/newbot` and follow the prompts to choose a name and a username.
3. Copy the token that `@BotFather` returns. It looks like `123456:ABC-DEF...`.

## Step 2: Connect the bot in XTM One

1. Go to `Settings` > `Channels & Bots` and click `Configure` on the Telegram card.
2. Paste the token into `Bot Token / Access Token`.
3. Choose any string as a webhook secret and paste it into `Webhook secret token (you define this; pass the same value as secret_token to setWebhook)`.
4. Click `Save Channel`, and copy the webhook URL shown on the card.

## Step 3: Register the webhook with Telegram

Telegram has no console for this — you register the webhook with one API call, using the same secret you just saved:

```bash
curl -X POST "https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook" -d "url=<YOUR_WEBHOOK_URL>" -d "secret_token=<YOUR_SECRET>"
```

Replace `<YOUR_TOKEN>` with the bot token, `<YOUR_WEBHOOK_URL>` with the URL from the Telegram card — it ends with `/api/channels/telegram/webhook` — and `<YOUR_SECRET>` with the webhook secret.

!!! warning "The secret is what authenticates Telegram"
    Telegram sends the secret back with every update, and XTM One checks it. If you skip it, any request that reaches the webhook URL is accepted. If it does not match what is stored, every update is rejected.

Send a message to the bot to confirm the connection works.

## Step 4: Enable Telegram on an agent

Open the agent, go to `Configuration` > `Channels & Bots`, and enable Telegram:

- Turn on `Receive` so the agent answers incoming messages.
- Turn on `Publish` if the agent should send messages on its own.
- Decide whether private chats are allowed with `Allow private`, and whether people without an account may use the agent with `Allow anonymous`.

Whitelist entries are Telegram chat IDs.

## What to expect on Telegram

- Telegram does not expose an email address, so senders cannot be matched to an XTM One account automatically. Turn on `Allow anonymous users` if unknown senders should be served — the agent still receives the username and display name as context.
- Mentions are detected, so `Only when mentioned` works in groups. Thread continuation does not: Telegram has no thread identifier narrow enough to track, so a mention is needed for each new question in a group where mention-only is on.
- For the same reason, `/stop` is not available on Telegram. `/reset` works everywhere.
- Photos, documents, audio, video, voice messages, and round video messages sent to the bot reach the agent, with their caption as the message. A file sent without a caption is still answered. Telegram lets a bot download files of up to 20 MB: a larger file is not passed on, but its caption is. Stickers are ignored.
- By default, `@BotFather` hides group messages from bots. If the bot should read group conversations, send `/setprivacy` to `@BotFather` and disable privacy mode for it.

## What the agent can do in Telegram

When `Publish` is on, the agent can send a message, send a photo by URL, edit or delete one of its own messages, forward a message between chats, pin or unpin a message, create a poll, and read basic information about a chat, group, or channel.

Pinning and deleting other people's messages requires the bot to be an administrator in the chat.

## Best practices

- Keep the webhook secret in place — it is the only thing authenticating incoming updates.
- Use a dedicated agent for Telegram when the audience is external, since senders are usually anonymous.
- In groups, prefer `Only when mentioned` so the bot stays quiet during normal conversation.

## Troubleshooting

**Nothing arrives.** Check the webhook is registered by calling `getWebhookInfo` on the Bot API. It reports the current URL and the last delivery error.

**Everything is rejected.** The `secret_token` passed to `setWebhook` does not match the value stored in XTM One. Re-run `setWebhook` with the right secret.

**The bot only sees messages that mention it.** Privacy mode is on in `@BotFather`, or `Only when mentioned` is on in XTM One.

**The bot cannot pin or delete.** It is not an administrator in that chat.

## Next step

Connect another platform, such as [Discord](discord.md), or review how agents use channels in [Agents](../../usage/agents.md).
