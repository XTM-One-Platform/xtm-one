# Discord

Connecting Discord lets people talk to an agent in a server channel or a direct message, and lets agents post messages, reactions, and threads back.

Discord is the one channel that does not use a webhook: the platform opens a persistent connection to Discord itself. That changes one thing in the setup — the connection starts when the platform starts.

Read [Channels and bots](index.md) first for the concepts shared with the other platforms.

## Before you start

- You need permission to create an application in the Discord developer portal, and to invite a bot to the target server.
- You need administrator rights in XTM One.
- You need to be able to restart the platform after saving the credentials.

## Step 1: Create the Discord application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and create a new application.
2. Open `Bot`, click `Reset Token`, and copy the token.
3. Under `Privileged Gateway Intents`, enable `Message Content Intent`. Without it, Discord delivers empty message content and the agent has nothing to answer.
4. Optionally copy the application public key from `General Information` if you plan to use slash commands.

## Step 2: Invite the bot to your server

1. Open `OAuth2` > `URL Generator`.
2. Select the `bot` scope.
3. Select the permissions `Send Messages`, `Read Message History`, and `Add Reactions`.
4. Open the generated URL and invite the bot to the server.

## Step 3: Connect the bot in XTM One

1. Go to `Settings` > `Channels & Bots` and click `Configure` on the Discord card.
2. Paste the bot token into `Bot Token / Access Token`.
3. Optionally paste the application public key into `Application public key`. It is only needed for the interactions endpoint used by slash commands, and it enables signature verification on that endpoint.
4. Click `Save Channel`.

!!! warning "Restart the platform after saving"
    The Discord connection is opened at platform startup. A bot saved on a running platform stays offline until the next restart.

## Step 4: Enable Discord on an agent

Open the agent, go to `Configuration` > `Channels & Bots`, and enable Discord:

- Turn on `Receive` so the agent answers incoming messages.
- Turn on `Publish` if the agent should post on its own.
- Decide whether direct messages are allowed with `Allow private`, and whether people without an account may use the agent with `Allow anonymous`.

Whitelist entries are Discord channel IDs. To copy one, enable developer mode in Discord, then right-click a channel and choose `Copy Channel ID`.

## What to expect on Discord

- Discord does not expose an email address, so senders cannot be matched to an XTM One account automatically. Turn on `Allow anonymous users` if unknown senders should be served — the agent still receives the username as context.
- Mentions are detected, so `Only when mentioned` works in server channels. Thread continuation does not: Discord has no thread identifier narrow enough to track, so a mention is needed for each new question when mention-only is on. `/stop` is unavailable for the same reason, while `/reset` works.
- Answers longer than the Discord limit are split into several messages automatically.

## What the agent can do in Discord

When `Publish` is on, the agent can send a message, read recent messages in a channel, list the channels it can see, create a thread, add a reaction, pin or unpin a message, and delete one of its own messages.

## Best practices

- Restrict the receive whitelist to the channels where the bot is genuinely expected, since a server invitation exposes many channels at once.
- Use `Only when mentioned` in community servers so the bot does not answer general chatter.
- Use a dedicated agent with a narrow tool set when the server is public.

## Troubleshooting

**The bot appears offline.** The platform has not been restarted since the credentials were saved, or the token is wrong. Restart and check the platform logs for the Discord connection.

**Messages arrive empty or are ignored.** `Message Content Intent` is not enabled in the developer portal.

**The bot answers in direct messages but not in channels.** Check the channel permissions on the Discord side, the receive whitelist, and whether a mention is required.

**Slash commands do nothing.** The interactions endpoint URL must be configured in the developer portal, and the application public key must be stored in XTM One — Discord probes the endpoint with an invalid signature and expects it to be refused.

## Next step

Connect another platform, such as [Google Chat](google-chat.md), or review how agents use channels in [Agents](../../usage/agents.md).
