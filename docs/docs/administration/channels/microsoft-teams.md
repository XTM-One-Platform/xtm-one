# Microsoft Teams

Connecting Microsoft Teams lets your users chat with an agent from a team channel, a group chat, or a personal chat, and lets agents post messages, cards, and files back into Teams.

Teams has the longest setup of all channels because it involves three Microsoft surfaces: an Entra ID app registration for the identity and permissions, an Azure Bot resource for message delivery, and a Teams app package for installation in the tenant. Read [Channels and bots](index.md) first for the concepts shared with the other platforms.

## Before you start

- You need an Entra ID (Azure AD) administrator, or someone who can grant admin consent for application permissions.
- You need permission to upload a custom app to your Teams tenant, or a Teams administrator to do it for you.
- You need administrator rights in XTM One.
- The platform must be reachable from the internet over HTTPS, because Teams posts messages to it.

## Step 1: Register the application in Entra ID

1. In the Azure portal, go to `Entra ID` > `App registrations` > `New registration`.
2. Note the `Application (client) ID` and the `Directory (tenant) ID` from the overview page.
3. Under `Certificates & secrets`, create a client secret and copy its value immediately — it is shown only once.
4. Under `API permissions`, add the Microsoft Graph application permissions listed below, then click `Grant admin consent`. They must be application permissions, not delegated ones.

### Application permissions

| Permission | Why it is needed |
|------------|------------------|
| `Channel.ReadBasic.All` | List the channels the bot can work with |
| `Chat.Create` | Start a chat with a user |
| `Chat.Read.WhereInstalled` | Read messages in chats where the app is installed |
| `Chat.ReadWrite.WhereInstalled` | Read and write messages in those chats |
| `ChatMember.Read.WhereInstalled` | Read the members of those chats |
| `Team.ReadBasic.All` | List the teams the bot can work with |

## Step 2: Create the Azure Bot resource

1. Create an `Azure Bot` resource in the Azure portal.
2. Choose `Single Tenant` and `Use existing app registration`, using the application ID from step 1.
3. In `Settings` > `Configuration`, set the messaging endpoint to the webhook URL shown on the Teams card in XTM One — it ends with `/api/channels/teams/messages`.
4. In `Channels`, add the `Microsoft Teams` channel.

## Step 3: Connect the bot in XTM One

1. Go to `Settings` > `Channels & Bots` and click `Configure` on the Microsoft Teams card.
2. Paste the application ID into `App ID / Client ID`.
3. Paste the client secret into `App Password / Client Secret`.
4. Paste the directory ID into `Entra ID (Azure AD) Tenant ID`.
5. Click `Save Channel`. The card shows `Connected` and displays the webhook URL used in step 2.

The application ID is also what authenticates incoming traffic: XTM One validates the signed token on every Teams request against it, so requests that do not come from the Bot Framework are rejected.

## Step 4: Build and install the Teams app package

Teams needs an app package to install the bot in your tenant.

1. Open the [Teams Developer Portal](https://dev.teams.microsoft.com/apps) and create the app.
2. Use the manifest template offered by the `Setup Guide` button on the Teams card — it already carries your deployment name — and replace the placeholders: `<YOUR_APP_ID>` with the application ID from step 1 (in `id`, `bots[0].botId` and `webApplicationInfo`), `your-domain.com` with your platform's domain, and the `developer` block with your organization.
3. Keep the resource-specific consent permissions listed below under `authorization.permissions.resourceSpecific`.
4. Add two icons: `color.png`, 192×192 in full color, and `outline.png`, 32×32, a white outline on a transparent background.
5. Zip `manifest.json`, `color.png`, and `outline.png` together, at the root of the archive: no folder inside the `.zip`.
6. Install it through the Teams admin center, or sideload it if your tenant allows custom apps. When you change the manifest later, raise its `version` so Teams clients pick up the new package.

The template targets manifest schema 1.29 and sets two properties that matter:

- `supportsChannelFeatures: "tier1"` lets members add the app to shared and private channels. It is mandatory for an app with the `team` scope on schema 1.25 and later. Do not also declare `supportedChannelTypes`: the two are mutually exclusive, and Teams rejects a package that declares both with "We can't read the manifest file".
- `supportsFiles: true` lets the bot receive files sent in personal chats. Files shared in group chats and channels are retrieved through Graph and work regardless of this setting.

### Resource-specific permissions

| Permission | Why it is needed |
|------------|------------------|
| `ChannelMessage.Read.Group` | Read channel messages in teams where the app is installed |
| `ChatMessage.Read.Chat` | Read messages in chats where the app is installed |
| `ChannelMember.Read.Group` | Receive membership events of the shared and private channels the app is added to |

## Step 5: Enable Teams on an agent

Open the agent, go to `Configuration` > `Channels & Bots`, and enable Microsoft Teams:

- Turn on `Receive` so the agent answers Teams messages, and optionally restrict it to specific channels.
- Turn on `Publish` if the agent should post to Teams on its own, and pick the channels it may post to.
- Adjust `Allow anonymous`, `Allow private`, and `Only mention` within what the bot allows.

The channel picker resolves team and channel identifiers for you. Group chats are never listed — their identifiers cannot be enumerated — so they are governed by `Allow private`, `Only mention`, and agent routing instead of by whitelists.

## Standard, private, and shared channels

Teams delivers messages differently depending on the channel type, and XTM One compensates for it.

- In **standard** channels, every message is delivered to the bot, so mention behavior is entirely up to your settings.
- In **private** and **shared** channels, Microsoft only delivers messages that @mention the bot. After the bot has answered a mention there, XTM One subscribes to that thread through Graph so follow-up messages keep reaching the agent without a new mention. Monitoring stops after seven days of inactivity, and the bot posts a short notice when it does.

## Graph notifications for private channels

Follow-ups in private and shared channels reach XTM One as Microsoft Graph notifications on `<BASE_URL>/api/channels/teams/graph-notifications`, a public endpoint Microsoft must be able to reach. Every subscription XTM One creates carries a random secret that Microsoft sends back with each notification; XTM One stores it encrypted and checks it, together with the subscription id, against what it recorded for the channel. Every notification of a delivery is checked before any work is queued for it, so one the verification policy below refuses costs nothing beyond that check. In the default mode, a forged notification that carries what earlier releases accepted (such as the `copilot_` value for a channel with no record) is still acted on: one task per distinct message it names, the same message named twice in one delivery counting once. A delivery larger than 4 MiB, far above what Microsoft sends, is refused with HTTP 413, and one XTM One cannot check or record because its Redis does not answer gets HTTP 503, so Microsoft delivers it again later. A follow-up posted while no subscription could notify it - a subscription Microsoft removed, or one XTM One had to recreate - is read back from Microsoft Graph once the channel has a subscription again, up to four hours back, and answered once.

By default, XTM One also acts on a notification that earlier releases acted on without that proof: one carrying the secret of the subscriptions created before 2026-07-02 (`copilot_` followed by the team id, which anyone who knows the team id can compute), or the channel's secret under another subscription id. Each case is logged as a warning, at most once every 15 minutes, and reported by the security audit in `Settings` > `Security`. Subscriptions created by an earlier release are kept and renewed as before, so nothing changes for your channels on upgrade.

To refuse every notification that does not prove its subscription, turn on `Require verified integration notifications` in `Settings` > `Security` > `Security options`, or set `INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true` - the switch that also refuses the unverified Slack, Microsoft Graph and LinkedIn notifications of integrations. The maintenance pass, which runs shortly after XTM One starts and then about once an hour, then replaces the subscriptions whose secret is derivable or can no longer be decrypted. Microsoft allows one subscription per channel, so it deletes the old one before creating the new one; what the old one was still delivering is accepted for four hours, and what was posted in between is read back from Microsoft Graph.

!!! warning "If Microsoft refuses the replacement"
    With the option on, a replacement deletes the old subscription first. If Microsoft Graph then refuses to create the new one, that channel's follow-ups stop until a later pass creates it, and the log says `Graph subscription creation failed`. With the option off, no subscription is deleted to be replaced.

## What the agent can do in Teams

When `Publish` is on, the agent gets Teams tools: send a message or an adaptive card to a channel, a personal chat, or a group chat, edit a message it already sent, read channel messages, chat messages, or thread replies, list teams and channels, list chats, upload a file to a channel, download a file it received, and delete one of its own channel messages.

Chat and group chat messaging is not governed by channel whitelists — those cover teams and channels. Long answers are split automatically, and images are delivered as separate cards because Teams rejects an activity carrying both text and attachments.

## Pasted XTM One links

A link to an assignment run (`<your XTM One address>/operations/executions/<id>`) pasted in a Teams chat or channel turns into a card. The manifest declares it in `composeExtensions` with a `link` message handler whose `domains` is your XTM One host, the same host as `validDomains`; the `Setup Guide` template includes it.

- The posted card names nothing: no run, agent or status. Each person who views it then gets their own version, rendered with their XTM One access, and Teams shows it to them alone.
- Someone who cannot see the run reads "This run doesn't exist or you don't have access to it", which is also what a link to a run that does not exist shows. A Teams user whose email matches no active XTM One user is asked to sign in with it.
- When the run waits for a person, whoever may answer it in XTM One - the run's owner, or an administrator for any run - gets the tools it wants to run, with an `Approve` / `Reject` choice per tool and an optional reason, or the agent's question with an answer box. Others see the state without the buttons. The answer goes through the same checks as the run page in XTM One, is recorded with the person who gave it, and the run resumes.
- Someone who sees the card but cannot answer the run can choose **Ask the owner to act** (**Ask an admin to act** on a company-managed run). The people who can answer it get a notification in XTM One, and the request is listed on the run page until the run is answered or they dismiss it. Nothing is shared: the person who asked still cannot see the run, and the card answers the same whether the run exists or not.
- In a conversation of up to 60 members Teams loads each viewer's card on its own; above that, a viewer chooses **Refresh card** in the message menu.

The app must be installed for the person who pastes the link (deploy it to your whole organization), and a manifest from before this feature must be updated in the Teams admin center. The Teams bot must have its `Tenant ID` set in XTM One: a card is only rendered and answered for people of that Entra tenant, so someone from another organization whose email matches an XTM One account is never taken for that account. Without it, links unfurl into the neutral card only.

## Best practices

- Grant admin consent once and keep the client secret in a password manager. When it expires, Teams stops delivering messages with no other symptom — use `Edit credentials` to paste the new value.
- Note the secret expiry date in your calendar. Entra ID secrets are time-limited and this is the most common cause of a bot going silent months after a working setup.
- Use `Only when mentioned` for busy team channels; thread continuation keeps follow-up questions working without a new mention.
- Keep the publish whitelist to the channels the agent genuinely reports into.

## Troubleshooting

**The bot is installed but never answers.** Check the messaging endpoint in the Azure Bot resource, confirm the Teams channel is added there, and confirm an agent has Teams `Receive` enabled.

**Teams refuses the package with "We can't read the manifest file".** Check the package against [Step 4](#step-4-build-and-install-the-teams-app-package): the files at the root of the `.zip`, the icon sizes, and `supportsChannelFeatures` without `supportedChannelTypes`.

**It worked and suddenly stopped.** The client secret has almost certainly expired. Create a new one and update it in XTM One.

**The bot answers in standard channels but not in a private one.** That is expected until the bot has been @mentioned in the thread once. After that first mention, follow-ups are picked up for seven days.

**Private-channel follow-ups after an upgrade or a `SECRET_KEY` change.** By default nothing changes: a subscription created by an older release keeps working and is renewed, and after a `SECRET_KEY` change a notification naming the subscription recorded for its channel is still acted on, with a warning. With `Require verified integration notifications` on, such subscriptions are replaced by the maintenance pass instead (see [Graph notifications for private channels](#graph-notifications-for-private-channels)). Nothing needs to change in Teams or Entra ID: the same permissions and the same public endpoint are used. Microsoft must be able to reach `<BASE_URL>/api/channels/teams/graph-notifications`, which also receives the subscription's lifecycle notifications.

**Messages are rejected as unauthorized.** The application ID stored in XTM One does not match the app the Azure Bot resource uses. They must be the same registration.

**The bot cannot read files sent in a chat.** Confirm `supportsFiles` is set in the manifest for personal chats, and that admin consent was granted for the Graph permissions used to fetch files shared in channels.

**A pasted run link stays a plain link.** The app is not installed for the person who pasted it, or its manifest has no `link` message handler for your XTM One host. Update the app package from the `Setup Guide` template.

**A pasted run card shows no details.** Check that the Teams bot has its `Tenant ID` set in XTM One. In a conversation of more than 60 members, choose **Refresh card** in the message menu. Otherwise, check that the person viewing it has an XTM One account with their Teams email address.

**Mentions are not detected in group chats.** Teams sometimes strips the mention data from the delivered message. XTM One re-reads the message through Graph to recover it, which requires the resource-specific permissions from step 4 to be declared and consented.

## Next step

Connect another platform, such as [Slack](slack.md), or review how agents use channels in [Agents](../../usage/agents.md).
