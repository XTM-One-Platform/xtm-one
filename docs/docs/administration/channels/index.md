# Channels and bots

A channel is a messaging platform your users already work in. When a channel bot is connected, people can talk to an XTM One agent from Slack, Microsoft Teams, WhatsApp, Telegram, Discord, or Google Chat instead of opening the web interface, and agents can post messages back into those conversations.

Everything on this page is configured in `Settings` > `Channels & Bots`. The per-platform guides then walk through the external setup, one platform at a time.

## Supported channels

| Channel | Direction | What you connect |
|---------|-----------|------------------|
| Web chat | Built in | Nothing — always available for every user and agent |
| [Slack](slack.md) | Receive and publish | A Slack app with a bot token and signing secret |
| [Microsoft Teams](microsoft-teams.md) | Receive and publish | An Entra ID app registration, an Azure Bot resource, and a Teams app package |
| [WhatsApp](whatsapp.md) | Receive and publish | A Meta app using the WhatsApp Business Cloud API |
| [Telegram](telegram.md) | Receive and publish | A bot created with `@BotFather` |
| [Discord](discord.md) | Receive and publish | A Discord application with a bot token |
| [Google Chat](google-chat.md) | Receive and publish | A Google Chat app and a service account key |

Web chat is listed in the same screen as `Web Chat`, with an `Always Active` badge. It needs no setup and cannot be disabled.

!!! note "Bots answer as a bot"
    A channel bot has its own identity: it hears what is addressed to the bot. To let an agent operate a **real user account** instead - answering when *you* are mentioned or messaged, or running a dedicated AI user that follows whole channels - see [Agent accounts](agent-accounts.md).

## Before you start

- You need to be an administrator. Only admins can create, edit, or delete channel bots.
- The deployment needs an active Agentic AI license. Without it, saving a channel bot is rejected.
- The platform must be reachable from the internet over HTTPS. Every platform except Discord delivers messages to a webhook URL built from `BASE_URL` — see [Configuration](../../deployment/configuration.md). A local or private address works for testing only if the messaging platform can reach it.
- You need administrator rights on the messaging platform itself, or someone who has them. Most steps happen in the external console, not in XTM One.

## How a channel goes live

The same four steps apply to every platform.

1. **Create the app on the messaging platform.** Each platform has its own console — the Slack app directory, the Azure portal, Meta for Developers, and so on. This is where you obtain the credentials and grant permissions.
2. **Connect the bot in XTM One.** In `Settings` > `Channels & Bots`, select the platform and click `Configure`, then paste the credentials. Every value is encrypted before it is stored. The card switches to `Connected` once credentials are saved.
3. **Point the platform at the webhook URL.** The connected card shows a `Webhook URL` you can copy under `Receive`. Paste it into the messaging platform's webhook or messaging endpoint setting. Discord is the exception — it opens its own outgoing connection and needs no webhook.
4. **Enable the channel on an agent.** A newly connected bot does not answer anything yet. Open the agent, go to `Configuration` > `Channels & Bots`, enable the channel, and turn on `Receive`, `Publish`, or both.

!!! tip "Use the built-in setup guide"
    Each card in `Settings` > `Channels & Bots` has a `Setup Guide` button. It shows the same steps as the pages here, with the permission list and your deployment's real webhook URL already filled in — including a copy-ready Teams manifest.

## Receive and publish

Every connected bot has two independent capabilities, shown side by side on the bot card and again on each agent.

- `Receive` covers incoming messages: someone writes in a channel or sends a direct message, and an agent answers.
- `Publish` covers outgoing messages: an agent uses the bot's tools to post a message, reply in a thread, add a reaction, or upload a file on its own initiative — for example at the end of an assignment.

The two are separate on purpose. An agent can post daily summaries to a channel without ever listening to it, and it can answer questions in a channel without being allowed to post anywhere else.

The `Publish` capability comes from the bot tools listed on the card. Click the tool count to browse them and test one against the live connection before an agent uses it.

## Restricting which channels a bot can use

A bot has ambient authority: on the messaging platform it can reach every channel it has been invited to. Channel whitelists narrow that down inside XTM One.

Both lists are edited on the bot card and apply to every agent using the bot:

- The `Receive` whitelist limits which channels the platform accepts incoming messages from. Messages from anywhere else are ignored silently.
- The `Publish` whitelist limits which channels agents can send to. A blocked send fails with an explicit access-denied error rather than posting to the wrong place.

When a list is empty, no restriction applies at bot level — but a bot with no whitelist at all is reserved for company-managed agents. Non-admin users only see the bot on their own agents once at least one whitelist entry exists, so they cannot silently give a personal agent workspace-wide reach.

Each agent can then narrow the bot's scope further in its own `Channels & Bots` section. The effective scope is the overlap between the agent's selection and the bot whitelist, never more than the bot allows. Admins can grant an exception per agent with `Bypass whitelist`. A message the whitelist would have dropped is then let in for those agents only: another agent, or the General Assistant, does not answer it.

## Tool approvals through a bot

A conversation on an external channel cannot show an approval prompt. When an agent answering through a bot reaches for a tool that needs approval, the bot replies with a refusal and the turn ends: the tool is not delayed, it is unavailable.

The `Ask before running tools` switch on each bot card decides this. It is on by default.

- On: tools that need approval stay unavailable in conversations through this bot, and the bot's own send, edit and delete tools ask for approval wherever an agent calls them, including from an assignment.
- Off: nothing an agent does in a conversation through this bot waits for approval, including tools that change or delete data outside the chat, and the bot's own message tools run without asking everywhere.

Turning it off asks for confirmation, and the change is recorded in the audit log. The security audit in `Settings` > `Security` lists every bot with the switch off. The bot's read tools are never exempted from approval automatically, since reading a channel means reading people's conversations: this switch is the only way to let them run without asking.

## Direct messages, mentions, and anonymous users

Three toggles decide when a bot answers at all. Each exists on the bot card and again on every agent, and the bot setting always wins.

| Toggle | What it does | Precedence |
|--------|--------------|------------|
| `Allow private conversations` | Direct messages reach the agent even when receive channels are restricted | If the bot has it off, no agent can turn it back on |
| `Only when mentioned` | In group channels, the bot answers only when it is explicitly @mentioned (not supported on WhatsApp) | If the bot has it on, no agent can turn it off |
| `Allow anonymous users` | People without an XTM One account can talk to the agent | If the bot has it off, no agent can turn it back on |

A few consequences are worth knowing:

- Direct messages always bypass `Only when mentioned`. Someone messaging the bot privately never has to mention it.
- With `Only when mentioned` on, once the bot has answered in a thread it keeps following that thread for about 24 hours without needing a new mention. This works where the platform reports both mentions and threads — Slack, Microsoft Teams, and Google Chat. On Telegram and Discord the mention is detected but there is no thread tracking, so each new question needs its own mention.
- `Only when mentioned` cannot be used on WhatsApp at all: the Cloud API does not report structured mentions, so the condition is never satisfied and the bot stays silent in groups. Leave it off there and scope the bot with the receive whitelist instead.
- With `Allow anonymous users` on, the conversation is not attached to a user account. The agent still receives the sender's display name and platform identity as context. The General Assistant is never available anonymously — anonymous traffic is served by agents only.
- An anonymous conversation reaches what the agent is configured with: its knowledge bases, integrations, handover targets and work-item peers, whatever their `Restrict access` setting. What it browses across the platform - the agent builder's read tools, template search - is limited to what every user sees: company-managed resources without `Restrict access`.
- Anonymous messages count against the platform-wide agentic limits of your license (no per-user limit applies to them). When the platform is at its limit, the sender is told `This service is at capacity right now. Please try again later.` wherever the bot would otherwise have answered - where it stays silent, it stays silent. See [Requests with no account](../settings.md#requests-with-no-account).

!!! warning "Anonymous access is a public door"
    Turning on `Allow anonymous users` lets anyone who can reach the channel use the agent, its tools, and its knowledge bases. Restrict the agent's tools and knowledge accordingly, and prefer a dedicated agent for public channels.

## Commands inside a conversation

Users have two commands on external channels, and they are worth mentioning when you announce a new bot:

- `/reset` or `/new` archives the current conversation and starts a fresh one, which is the way to clear context that has drifted. In a direct message a web conversation was continued in, it ends the continuation instead: the web conversation is kept, and the next message starts a fresh one.
- `/stop` mutes the bot in the current thread for seven days. Mentioning the bot again lifts the mute immediately. It works where threads are tracked — Slack, Microsoft Teams, Google Chat, and WhatsApp groups. On WhatsApp the mute cannot be lifted early, since mentions are not detected there, so it lasts the full seven days.

## One conversation across the web, Slack, and Teams

A user can continue the same conversation in the web app and in a direct message with the Slack or Teams bot, in either direction (see [Chat](../../usage/chat.md#continue-a-conversation-in-slack-or-teams-or-back-on-the-web)). It stays one record: its history, compaction summary, files, pending approvals, agent and session tools.

- Only direct messages are shared with the web: a Slack DM thread and the Teams personal chat with the bot. Channel threads and group chats are never listed on the web nor continued from it, because other people read them: the web chat, its file upload and the platform chat API refuse them (`409`). The platform chat API, which runs its turns without the conversation's turn lock, also refuses a Slack or Teams direct-message conversation and a web conversation continued in one.
- A direct message continues a conversation only when its sender resolves to the conversation's owner. A sender the bot cannot match to an account, or matches to someone else, never reaches it.
- The conversation keeps its agent. That agent must have the channel enabled with `Receive` and `Publish` for the continuation to be offered and answered; a General Assistant conversation answers in any direct message.
- The continuation is offered, and accepted by the API, only where the bot would answer the direct message: it is checked with the same gates the bot applies to every message it receives (the receive whitelist and `Allow private conversations`, an agent's `Bypass whitelist`, `Only when mentioned`, then the agent's own channel settings). A Slack bot with a receive whitelist and private conversations off drops direct messages, so the option is not offered. A Teams personal chat is never filtered by the whitelist.
- One turn runs at a time on a conversation, whatever the channel. A Slack or Teams direct message that arrives while a turn runs is queued, acknowledged at once, and answered as the next turn, in arrival order; consecutive messages of one thread are answered together. Up to five messages wait per conversation, for at most an hour; a redelivered event (a Slack retry, a Teams redelivery) is never queued or answered twice. If the process running a turn stops, the scheduler restarts the queue within a minute of that turn's lock expiring, and a queued message that process was answering is answered again, unless its turn had already stored it - a message is started at most twice and stored once. A queued message the bot drops without an answer (it could not be answered twice, or it waited more than an hour) is told so in its thread, in the sender's language. A queued message waits in Redis, its text and the sender's email included, for at most that hour. The files of a queued message that is never answered are deleted. The web refuses a message while a channel turn runs, and the web `Stop` also stops a turn started from a channel. Direct messages on WhatsApp, Telegram, Discord and Google Chat are not queued.
- A tool call waiting for approval is decided in the web app. A channel message sent while the turn that proposed it still waits is queued behind it; one that arrives after that turn ended retires the call, as a new web message does, and the reply says so.
- Continuing in Slack uses the scopes the bot already needs (`users:read.email` to find the user, `chat:write` and `im:write` to open the direct message). Continuing in Teams needs the user to have written to the bot once in a personal chat: Teams lets a bot write first only where it already has a conversation reference.
- Each continuation is recorded in the audit log (`bind` / `unbind` on the conversation).

## Files in replies

When an agent's answer on a channel includes a file — a report, an export, a generated image — the message carries a link to it. The link works without an XTM One sign-in for 24 hours, so treat it like the file itself.

- PDFs, plain-text, Markdown and CSV files, WebVTT subtitles, JSON files (JSON Lines and `+json` types such as JSON-LD included), audio, video, and images (PNG, JPEG, GIF, WebP, AVIF, BMP, TIFF, ICO, HEIC / HEIF, JPEG XL, JPEG 2000) open in the browser. A browser that cannot display or play a format, such as TIFF or HEIC outside Safari, or CSV and JSON Lines in Chrome, saves it instead.
- HTML pages, SVG drawings, XML files, and every other type are downloaded, never opened: a file an agent or a user wrote can never run as a page on your platform's address.

## How the bot knows who is talking

XTM One matches the sender to an account by email address:

- Slack and Google Chat provide the email directly, so matching is automatic.
- Microsoft Teams provides the email in most messages, and otherwise the platform resolves it from the sender's Entra ID identity.
- WhatsApp, Telegram, and Discord do not expose an email. Those senders are either treated as anonymous, when the bot allows it, or asked to register.

Once a sender has been matched by email, XTM One also remembers their identity on that platform, so later messages match even without an email. A WhatsApp, Telegram, or Discord sender who never provides an email can be linked to an account by an administrator, through the API only (`/api/v1/users/{user_id}/channel-identities`). There is no screen for it.

When the sender matches an account, the conversation inherits that user's permissions, personal preferences, and memory — the same as if they had used web chat. Users can also route their own direct messages to a specific agent in `My Profile` > `Channels & Bots`.

## The language of the bot's own messages

Besides the agents' answers, a bot sends short messages of its own: that a message is queued behind the one being answered, that a quota or the run's budget is reached, that an agent is not available on the channel, that something went wrong, the first message of a conversation continued from the web, and the like. The link it adds for a file, presentation or report an agent produced with no name is labelled the same way. They are written in English, French, Spanish or Japanese:

- A sender matched to an account gets the language set in their profile.
- A sender with no language of their own (`Auto`), a sender the bot does not match to an account, and the few replies sent before the bot looks the sender up (AI switched off, `/stop`, the platform briefly unable to tell whether AI may run) get the platform's default language from `Settings` > `Parameters`.
- When neither is set, they are in English.

The language is never guessed from what the sender writes. The agents' answers are not affected: an agent answers in the language its persona and the conversation lead it to. In the web app, the conversation keeps the platform's own wording of a refusal or an error; only what the channel shows is translated.

## Good habits

- Connect one bot per platform and share it. Several agents can use the same bot with different scopes.
- Set a receive whitelist before inviting the bot to busy channels, then widen it once the behavior is what you expect.
- Turn on `Only when mentioned` for shared team channels, and leave it off for a dedicated support channel where the bot should answer everything.
- Keep publish scopes tight. An agent that only needs to post a daily report needs one channel, not the whole workspace.
- Review `Routing` on the bot card: it lists exactly which agents receive from and publish to that channel.
- Rotate credentials on the messaging platform side, then paste the new value with `Edit credentials`. Fields left blank keep their stored value.

## Troubleshooting

**The bot never answers.** Check in this order: the bot card shows `Connected`; an agent has the channel enabled with `Receive` on; the channel is in the receive whitelist (or no whitelist is set); the bot has actually been invited to the channel. On group channels, also check whether `Only when mentioned` is on.

**The bot answers in direct messages but not in channels.** Almost always the receive whitelist, a missing invitation, or a missing mention.

**The messaging platform rejects the webhook URL.** Save the bot credentials in XTM One first. Verification requests are signature-checked, so the platform's URL test only succeeds once the secret is stored. Also confirm `BASE_URL` matches the public HTTPS address you pasted.

**The bot answers twice.** On Slack, this is caused by subscribing to both `message.channels` and `app_mention`. Keep one of the two.

**An agent cannot post to a channel.** The publish whitelist on the bot, the agent's own publish selection, or both. The effective scope is the overlap of the two.

**Nothing works after changing the platform URL.** The webhook URL is derived from `BASE_URL`. Update the endpoint on every messaging platform after the address changes.

## Next step

Pick the platform you want to connect: [Slack](slack.md), [Microsoft Teams](microsoft-teams.md), [WhatsApp](whatsapp.md), [Telegram](telegram.md), [Discord](discord.md), or [Google Chat](google-chat.md).
