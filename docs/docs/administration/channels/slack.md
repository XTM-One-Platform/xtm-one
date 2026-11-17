# Slack

Connecting Slack lets your teams talk to an agent from a channel or a direct message, and lets agents post into Slack on their own — for example to publish a triage summary or reply in an incident thread.

Read [Channels and bots](index.md) first if you have not already: whitelists, mention behavior, and anonymous access work the same on every platform.

## Before you start

- You need permission to create and install an app in your Slack workspace. In many workspaces this requires a Slack administrator to approve the installation.
- You need administrator rights in XTM One.
- The platform must be reachable from the internet over HTTPS, because Slack posts events to it.

## Step 1: Create the Slack app

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and select `Create New App` > `From scratch`. Give it a name and pick your workspace.
2. Open `OAuth & Permissions` and add every scope from the table below under `Bot Token Scopes`.
3. Open `Agents & Assistants` and turn on the `App Agent` toggle. This is what allows the typing indicator while an agent is thinking.
4. Install the app to the workspace, then copy the `Bot User OAuth Token` — it starts with `xoxb-`.
5. Open `Basic Information` and copy the `Signing Secret`.

### Required scopes

| Scope | Why it is needed |
|-------|------------------|
| `app_mentions:read` | Receive messages that @mention the bot |
| `assistant:write` | Show the typing indicator while the agent works |
| `channels:history` | Read messages in public channels the bot has joined |
| `channels:join` | Join public channels |
| `channels:read` | Read basic information about public channels |
| `chat:write` | Send messages as the bot |
| `groups:history` | Read messages in private channels the bot has joined |
| `groups:read` | Read basic information about those private channels |
| `im:history` | Read direct message history |
| `im:read` | Read basic direct message information |
| `im:write` | Start a direct message with someone |
| `users:read` | Read workspace profiles |
| `users:read.email` | Match Slack users to XTM One accounts by email |

Adding scopes after installation requires reinstalling the app.

## Step 2: Connect the bot in XTM One

1. Go to `Settings` > `Channels & Bots` and click `Configure` on the Slack card.
2. Paste the bot token into `Bot Token / Access Token`.
3. Paste the signing secret into `Slack signing secret for request verification`.
4. Click `Save Channel`. The card now shows `Connected` and displays the webhook URL to use in the next step.

!!! warning "Save the signing secret before configuring events"
    Slack signs its verification request, and XTM One refuses unsigned traffic. If the signing secret is not stored yet, Slack's URL check fails with a 401. Always complete this step before step 3.

## Step 3: Subscribe to events

1. Back in the Slack app, open `Event Subscriptions` and enable events.
2. Set the `Request URL` to the webhook URL shown on the Slack card — it ends with `/api/channels/slack/events`. Slack verifies it immediately.
3. Under `Subscribe to bot events`, add the events you need:

| Event | What it delivers |
|-------|------------------|
| `message.im` | Direct messages with the bot — always add this one |
| `app_mention` | Messages that @mention the bot in a channel |
| `message.channels` | Every message in the public channels the bot has joined |

The usual setup is `message.im` plus `app_mention`: the bot answers direct messages and mentions. For a dedicated support channel where the bot should answer everything, use `message.im` plus `message.channels` instead.

!!! warning "Never subscribe to both channel events"
    Subscribing to `message.channels` and `app_mention` together makes Slack fire two events for the same mention, and the bot answers twice.

## Step 4: Invite the bot to channels

Type `/invite @YourBotName` in each channel where the bot should be active. The bot only sees channels it belongs to.

## Step 5: Enable Slack on an agent

Open the agent, go to `Configuration` > `Channels & Bots`, and enable Slack:

- Turn on `Receive` so the agent answers incoming Slack messages, and optionally restrict it to specific channels.
- Turn on `Publish` if the agent should post to Slack on its own, and pick the channels it may post to.
- Adjust `Allow anonymous`, `Allow private`, and `Only mention` for this agent, within what the bot allows.

Channel whitelists on the bot use Slack channel IDs. The picker lists the channels the bot can see, so you rarely need to type an ID by hand.

## What the agent can do in Slack

When `Publish` is on, the agent gets Slack tools it can use during a conversation or an assignment: send a message, reply in a thread, read recent messages or a thread, list channels and users, look up a user, add or remove a reaction, pin or unpin a message, set a channel topic, and delete one of its own messages.

Replies are converted to Slack formatting automatically, so bold text and links render natively rather than as raw Markdown. Long answers are split into several messages instead of failing.

## Best practices

- Use `Only when mentioned` in busy team channels. The bot still follows a thread it has already joined for about 24 hours, so people do not have to mention it in every reply.
- Prefer a dedicated agent for public channels, with a narrow set of tools and knowledge bases.
- Set the receive whitelist before inviting the bot to many channels, so a wide invitation does not become wide exposure.
- Tell users about `/reset` to start a clean conversation and `/stop` to mute the bot in a thread that has moved on.

## Troubleshooting

**Slack rejects the request URL.** The signing secret is missing or wrong in XTM One. Save it on the Slack card, then retry the URL verification.

**The bot ignores channel messages.** Check that it was invited to the channel, that the right event is subscribed, that the channel is in the receive whitelist, and that an agent has Slack `Receive` enabled.

**Every mention gets two answers.** Both `message.channels` and `app_mention` are subscribed. Remove one.

**No typing indicator.** The `App Agent` toggle is off or `assistant:write` is missing. Everything else keeps working.

**The bot answers but cannot identify the user.** The `users:read` and `users:read.email` scopes are required to match a Slack user to an account. Without them, senders are unknown and are only served if anonymous access is allowed.

**A reply lands in the channel instead of the thread.** Slack refuses thread replies on some message types; the platform falls back to posting in the channel rather than losing the answer.

## Next step

Connect another platform, such as [Microsoft Teams](microsoft-teams.md), or review how agents use channels in [Agents](../../usage/agents.md).
