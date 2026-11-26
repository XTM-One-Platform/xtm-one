# Integrations

An integration connects XTM One to an external service, such as email, ticketing, messaging or cloud documents, and gives agents live access to it. This page gives you a quick tour of the integration screens.

Each first-party provider card in the integration catalog carries a diagonal corner ribbon. **Certified** marks the integrations Filigran qualifies and supports: the Filigran products (OpenCTI, OpenAEV, OpenCRQ and the XTM Hub) and the integrations Filigran runs its own agents on every day: Ashby, Atlassian Jira, Concord, DocuSign, Drata, Figma, GitHub, Google, Google Search, Grafana, HackerOne, HiBob, HubSpot, Letsignit, LinkedIn, Metabase, Microsoft, Microsoft Defender XDR, Microsoft Entra ID, Microsoft Intune, Microsoft Purview, Notion, OpenAPI / Swagger, Perk, Planhat, Slack and WhatsApp. The catalog serves the qualification as `is_certified` on each provider. **Preview** marks every other catalog entry. Custom providers you import yourself carry no ribbon — the qualification is ours to make about our own catalog, not about your bundles — and are identified by their `Custom` badge instead.

An integration of an imported custom provider calls its API at the base URL set in its configuration. Like an OpenAPI integration, it can reach public hosts and private-network hosts, so an intranet API works, but loopback, link-local, reserved and cloud-metadata addresses are refused. The base URL is checked when the integration is saved (a host name that does not resolve yet is accepted), and again on every tool call and `Test connection`, where each connection goes to the address that was checked. For an integration routed through a relay group, the relay checks the destination, and saving refuses only a base URL that is a cloud-metadata address.

## The list page

In the left navigation, open `Tools & Integrations > Integrations`. The list page answers one practical question fast: which external connections are usable right now.

Each card is status-heavy by design, so the list already works as a health dashboard. The status badge is the signal to read first — it distinguishes a connection that is ready, one that is configured but not yet authorized, and one that needs re-authorization because permissions changed or the provider refused its refresh token or client credentials. You can search by name or provider and filter by visibility, tags, or category to narrow a long list.

If you have permission, `New Integration` starts a new connection. Credentials are always encrypted in the database.

## The detail page

Opening an integration shows a header with the connection status and the actions that make sense for it — a setup guide for provider prerequisites, `Test connection` to validate the live connection, `Authorize` or `Re-authorize` to resolve OAuth state, and delete. The detail page itself is organized into `Overview`, `Configuration`, and `Activity` tabs.

A provider whose authentication modes are set up apart (Planhat: OAuth or an API key) has one setup guide per mode: the creation page shows the guide of the mode you pick, and an integration's page the guide of the mode it uses. The catalog card shows a guide that covers every mode.

When an integration is built on an open-source project, its setup guide credits that project under the title: its name links to the project's repository, followed by its author and its license. The Nova integration, for example, credits NOVA by Thomas Roccia, released under the MIT license; see [Prompt safety (Nova)](../../administration/prompt-safety.md).

Some integrations add pages of their own after those three tabs — the `API Spec` of an integration imported from an OpenAPI or Swagger document, or the rules of the Nova integration, which only administrators see. These pages are tabs of the integration: open the integration from `Tools & Integrations > Integrations` to reach them. Integrations are never listed one by one in the left navigation. Anyone who opens such a page from a link without being allowed to see it is told it is not available to them, with a way back to the `Overview`.

When `Test connection` reports a failure the platform recorded earlier - such as the last rule sync of the Nova integration - the stored error is shown only to the people who can manage the integration (administrators, for a company-managed integration). Everyone else sees that the check failed and a sentence saying who can see the details; the server log always keeps the full error.

### Overview

`Overview` tells you not only whether the integration works, but why it does or does not. It combines usage figures, the connection state (authentication method, credential readiness, authorized and missing scopes), and cards showing which agents — and, for sync sources, which knowledge bases — depend on it.

### Configuration

`Configuration` is a richer screen than most other resources because it mixes three concerns: the integration's identity, its authentication and provider setup, and its visibility and reuse in the workspace. The description field matters more than it looks — the UI passes it to agents as tool context.

This tab is also where you see what the integration actually does:

- The available tools are split into read tools (retrieve or inspect information) and write tools (change something in the connected system). That split helps you judge risk. When the integration is connected, you can test a tool with real credentials before binding it to an agent.
- If the provider supports events, an event triggers card explains, in business language, which external changes an agent can react to and how they are delivered (webhook, polling, or hybrid).
- A webhooks card shows how external events enter XTM One. Depending on the provider it can display a URL to copy or, for GitHub, let you create and remove repository webhooks directly from the UI. What it shows follows the provider and the OAuth app the integration uses:
    - Microsoft 365: new emails and Teams messages are picked up by polling every 5 minutes, so there is no webhook to configure.
    - Slack: new messages are picked up by polling every 5 minutes. For real-time events, the card shows the shared Slack notifications endpoint to set as the Request URL in the Event Subscriptions of the Slack app the integration uses; the integration's `Signing secret` field (or, on your administrator's Slack app, the Slack OAuth default's) is what verifies them. See [Slack message events](#slack-message-events) below.
    - Other providers with events: an integration created on your administrator's OAuth app (you left its client ID empty and the administrator configured OAuth defaults for the provider) gets its events through the webhook the administrator set up, so the card says so instead of showing a URL. An integration on your own OAuth app shows its own webhook URL to configure in that app.
    - Replacing the client ID moves an integration to your own OAuth app. The tokens issued to the previous app are removed, so authorize the integration again before it is used.
    - For integrations created before XTM One recorded which OAuth app they use, the card compares the integration's client ID with the administrator's current OAuth default: the same ID means the administrator's app, any other means your own. Only when that comparison cannot be made does the card follow whether the provider has OAuth defaults, as it did before.

### Slack message events

`New Slack message` assignments receive messages in two ways. The event poller reads the monitored channels every 5 minutes with the integration's own token. It needs no setup and serves every assignment whose trigger names the integration (the assignment editor always sets one) or whose agent is bound to it. For real-time delivery, the Slack app the integration runs on sends its Events API deliveries to `/api/v1/integrations/notifications/slack`.

That endpoint checks every delivery against the app's **Signing Secret** (Slack app > `Basic Information`). Nothing has to be set up for events to keep arriving: with no Signing Secret stored, a delivery is accepted without a verified signature, exactly as before signatures were checked, and XTM One says so with a warning in the log and a Security Audit finding. Storing the secret turns verification on for the integrations of that app:

- An integration running on its own Slack app: put the secret in the integration's `Signing secret` field.
- An integration created with the Slack app your administrator provides (Settings > OAuth defaults): the administrator stores the secret in the `Signing secret` field of the Slack OAuth default, once for everyone.

Once a Signing Secret is stored, a delivery it does not prove never reaches the integrations of that app, and a verified event reaches only what it concerns:

- It reaches only the integrations running on the app that signed it, and only those of the Slack workspace it was sent for.
- It then reaches the assignments whose agent is connected to one of those integrations, under the rules the poller applies: for an OAuth integration bound to the agent, the token is the agent owner's own, or shared with its owner's explicit acknowledgement. An assignment whose acting user can no longer see the integration still receives the event, as polled events are delivered, and the log says so.
- A message outside a public channel (private channel, direct or group message) reaches only the integration of its author or of the user Slack authorized the event for. Everyone else still receives it through the poller if their own token can read it.
- The trigger's channel selection applies, as the poller applies it.
- An assignment whose agent is connected to no Slack integration at all has no app or workspace to be matched against: it receives the event as before.

An event the platform cannot verify with what it holds (no usable Signing Secret for the app that sent it) is handed out as before signatures were checked: to every `New Slack message` assignment whose channel selection admits it, except those connected only to integrations whose app Signing Secret is stored.

Each message runs an assignment once. The real-time path and the poller claim it under the same key, and a Slack retry of the same event is skipped. A message an assignment's own run posted does not run that assignment again; any other assignment watching the channel still runs on it.

To refuse every event that cannot be verified, an administrator turns on **Require verified integration notifications** in Settings > Access and security (or sets `INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true`, which wins and locks the field). The messages of integrations without a stored Signing Secret then arrive through the poller only, and the Security Audit says how many integrations are concerned. If Slack disabled the app's Event Subscriptions after refused deliveries, re-enable them once the secret is stored.

### OpenRouter

The OpenRouter integration connects an OpenRouter account - one API key for hundreds of models. It takes the account's **API key** and, optionally, a **management key** (OpenRouter > Settings > Management keys): the management key cannot run models, but it unlocks the credits, the activity and the API key management below. `Test connection` checks both keys.

Its three pages, tabs of the integration shown to the people who manage it:

- `Usage` - what the key spent today, this week and this month, its spending limit, the credits left on the account, and the spend per day and per model over the last 30 days, with the providers that served each model.
- `Models` - the catalog the key may use (text and embedding models): prices per million tokens, context, tool calling, reasoning, image input, and for each model the providers serving it with their price, uptime and latency. An administrator uses it to feed the platform's **OpenRouter LLM provider**: offer the models you want, mark the default one, and create the provider - or link an OpenRouter provider made in `Settings > AI Models`. The provider keeps a copy of the integration's API key, replaced whenever the key is replaced in the integration, and the context window of every newly offered model is taken from OpenRouter's catalog. Only a company-managed integration can feed the platform provider: the provider is shared by every user. Making the integration private again, unlinking it or deleting it leaves the provider running on the key it holds. In `Settings > AI Models` such a provider carries an `Integration` badge that leads back here.

The integration's `Overview` says whether the platform runs on it: the provider it feeds and its models, or, for an administrator, what is missing and a link to the `Models` page that creates it. Adding an OpenRouter provider in `Settings > AI Models` while a company-managed OpenRouter integration holding its API key feeds none points there too, so the key is not entered a second time.

### LM Studio

The LM Studio integration connects an LM Studio server (0.4 or later). When the platform can reach the server - an LM Studio on the same machine or the same network - leave `Route through relay` on `Direct`: the platform calls the server itself. When it cannot, route the integration through a relay group, and every request leaves from a relay installed next to the server (see [Relays](relays.md)). It takes the **server URL** as the platform, or the relay, reaches it (for example `http://gpu-host:1234`, or `http://host.docker.internal:1234` for an LM Studio running on the machine that hosts the platform's containers; a `/v1` suffix is removed) and, only when *Require Authentication* is on in LM Studio's server settings, an **API token** (masked, never shown again). The integration is connected as soon as its server URL is set. `Test Connection` lists the server's models, on the integration's route.

Its `Models` page is for the people who manage the integration:

- the models on the server - chat, embedding and decision models, loaded or not - with their size, quantization, maximum context, tool calling, reasoning and image input, and each loaded instance with the context it is served with;
- `Load` a model with its options (context length, flash attention, KV cache on the GPU) and unload an instance; `Download a model` from the LM Studio catalog (`openai/gpt-oss-20b`) or a Hugging Face link, with a quantization for a Hugging Face link, and follow the download. Loading, unloading and downloading are for the holder of the server: an administrator for a company-managed integration, its owner otherwise;
- the platform's **LM Studio LLM provider**: an administrator offers the models they want, marks the default one and creates the provider from a company-managed integration. The provider holds no URL and no token: it reads them, and the relay group, from the integration on every request, so an edit of the integration applies to it at once: its requests go through the relay group when it has one, directly otherwise. A newly offered model gets the context it is loaded with (the smallest of its instances), else its maximum. Unlinking the provider, deleting the integration or making it private stops it: its requests are refused. An LM Studio provider is created only this way, never from `Settings > AI Models`.
- `API Keys` - the keys of the account (management key): their spend, limit and expiry. The holder of the account - an administrator for a company-managed integration, its owner otherwise - can create a key with its own spending limit and reset period, rename it, change its limit, disable it or delete it. A new key is shown once; XTM One does not keep it. The key the integration runs on, and the key of the platform LLM provider, cannot be disabled or deleted from here.

### Microsoft 365: Teams meeting recording and recurring events

The Microsoft integration's calendar tools act as the account that authorized it, on its default calendar or on any calendar passed as `calendar_id` (a secondary calendar, or one shared with the account).

- **Recording and transcription.** When an agent creates or updates a Teams meeting, it can turn on **Record and transcribe automatically**, and allow or block transcription and recording. The tool changes the event's Teams meeting, then reads the options back from Microsoft and reports what Teams kept: when Teams does not keep a change (a Teams meeting policy of the organizer can forbid recording or transcription), the agent gets an error, never a success. Reading an event also shows its Teams meeting options: recording, transcription, who can bypass the lobby and who can present.
- **Recurring series.** An agent can create a daily, weekly, monthly or yearly series, list its occurrences between two dates (with the ones already cancelled), and move or cancel one occurrence without touching the rest. Cancelling a meeting the account organizes sends its attendees a cancellation notice, with an optional message. A series has one Teams meeting, so its recording options cover every occurrence.

Only the organizer of a meeting can change its options, so connect the integration with the account that organizes the meetings (a dedicated service mailbox, for example). The options use the delegated Microsoft Graph permission `OnlineMeetings.ReadWrite`, which the integration requests; acting as the signed-in account, it needs no Teams application access policy. If a tool answers that the token lacks `OnlineMeetings.ReadWrite` (for example in a tenant where users cannot consent to it), an administrator adds it to the delegated permissions of the integration's app registration in Microsoft Entra ID and grants admin consent, then the integration is re-authorized (`Re-authorize` on its page) so its new token carries the permission.

### Microsoft 365: answering a meeting invitation

An agent can accept, tentatively accept or decline a meeting invitation the account received, as Outlook's buttons do, with an optional comment to the organizer. It answers the invitation only and never changes the meeting: a meeting the account organizes is refused, and a cancelled one too.

- **One occurrence or the series.** An occurrence of a recurring meeting (from the list of its occurrences) answers that occurrence only; the series answers every occurrence.
- **Without notifying the organizer.** The response is sent to the organizer by default; the agent can also record it in the calendar only.
- **Proposing another time.** With a tentative acceptance or a decline, the agent can propose a new time, sent to the organizer with the response, when the organizer allows new time proposals.
- **What the agent reports.** After answering, the tool reads the event back from Microsoft: its subject, start, end, the response Microsoft now shows (accepted, tentatively accepted or declined), whether the organizer was notified, and its link. A response Microsoft does not show is reported as not confirmed, never as done. When Outlook takes a declined meeting off the calendar, the agent says so.
- It is a write: an agent whose Microsoft binding is read-only does not get it. It uses the delegated permission `Calendars.ReadWrite`, which the integration already requests.

### Microsoft 365: telling a meeting message from an email

A meeting invitation reads like any email when its body holds no Teams join link, an in-person meeting above all. The mail tools say which messages are meeting messages, read from the type Microsoft gives the message, with no extra request.

- **Reading one email.** `Message type:` is `message` for an email, else `meetingRequest`, `meetingCancelled`, `meetingAccepted`, `meetingTentativelyAccepted` or `meetingDeclined`.
- **Listings and search.** The full listing and the search carry the same line; the one-line listing adds `meetingRequest`, `meetingCancelled` or `meetingResponse` to the message's state.
- **New email triggers.** The event carries `message_type`. The filter `exclude_meeting_messages` drops meeting requests, cancellations and answers before any run; `{"message_type": "meetingRequest"}` in an expression keeps only invitations.

### Microsoft 365: Teams meeting transcripts

An agent reads what was said in a Teams meeting from its transcript, as the account that authorized the integration: in meetings it organized and in meetings it was invited to. Each line names its speaker.

- **Why there is no transcript.** When Microsoft lists no transcript for a meeting, the agent gets Microsoft's reason and message: either Microsoft exposes no transcript for the meeting, or Microsoft refused to list it. A refusal is a permission problem, not a missing transcript, and the answer names the permission to grant, `OnlineMeetingTranscript.Read.All`. Listing recent transcripts counts the refused meetings apart from the meetings without a transcript.
- **From the recording.** A transcribed meeting also keeps its transcript inside its recording: it is what the Teams player shows. When Microsoft lists no transcript, the join-URL transcript tool reads the one inside the meeting's recording, in the `Recordings` folder of your OneDrive (whatever its name in your language), where Teams saves an automatic recording of a meeting you organize. The answer names the recording it read, and the other recordings made at that time if there are any.
- **From a recording link.** `get_microsoft_recording_transcript` reads the transcript of a recording from its OneDrive or SharePoint link (a sharing link or the address of the video), or finds a meeting's recording in your OneDrive from the meeting ID. Use the link for a recording saved in someone else's OneDrive: Teams saves a recording in the OneDrive of the person who started it.
- Only the transcript is read: the video is never downloaded.
- Reading the transcript inside a recording goes through SharePoint, so it needs the delegated permission `AllSites.Read` (Office 365 SharePoint Online) on the integration's app registration, like the transcripts of channel meetings. If the agent answers that it could not obtain a SharePoint access token, an administrator adds that permission in Microsoft Entra ID and grants admin consent, then the integration is re-authorized (`Re-authorize` on its page).

### Microsoft 365: reading a Teams message in full

When an agent lists a Teams channel or chat, it sees each message whole, up to 4,000 characters of text (an administrator can change this with `MICROSOFT_TEAMS_MESSAGE_TEXT_LIMIT`, see [Configuration](../../deployment/configuration.md#microsoft-365-integration)). A longer message, such as a memo with sections and lists, is cut at the end of a word and ends with a note: `[truncated, N more characters: read it in full with get_microsoft_channel_message(team_id=..., channel_id=..., message_id=...)]`. The note names the exact call, with the message's own IDs, so the agent can read the rest. A message without the note is complete.

- **One message, in full.** `get_microsoft_channel_message` reads one post or thread reply of a channel, and `get_microsoft_chat_message` one message of a chat. The agent gets the whole text (including cards posted by apps and workflows, and quoted messages), the author, when it was posted, edited or deleted, its reactions, attached files, pasted images, mentions and its Teams link. For a channel post, it also gets the number of replies and the IDs of the newest ones.
- **From a link.** Paste a Teams message link (`Copy link` on a message in Teams) in the conversation: the agent passes it to the tool, which reads the team, channel, message and thread from the link.
- **Busy channels.** All the messages of one listing share a text budget, so a channel with many long messages stays readable: the longest ones are cut shorter, the short ones stay whole, and every cut message carries its note.
- Both tools are reads. A Teams channel Scope(s) limits the channel tool to the selected channels (a link to another channel is refused) and removes the chat tool.

### Microsoft 365: posting a file in a Teams channel

An agent can post a file of its conversation or run in a Teams channel, as the account that authorized the integration: a deck it exported as a PDF, a report, a file a user attached. The tool is `send_microsoft_channel_file`, and the file is its `file_id`, the id the tool that produced or received the file returns (`export_presentation` for a deck).

- The file is uploaded unchanged into the channel's **Files** folder (its SharePoint folder), then the message is posted with the file attached, so it opens from the message like a file a person shared. A file of the same name in that folder is replaced, as a new version of it.
- The post is a new message with an optional text, or a reply in a thread (`reply_to_message_id`). One file per message, up to 100 MB.
- The agent gets back the link to the message and the file's SharePoint links.
- A file that cannot be uploaded posts nothing, and a channel that does not exist (or that the account cannot reach) is reported with nothing uploaded.
- It is a write: a read-only binding of the integration does not offer it, and a Teams channel Scope(s) limits it to the selected channels. The account must be a member of the team; the delegated permissions `ChannelMessage.Send` and `Files.ReadWrite.All`, which the integration requests, cover the post and the upload.

### Microsoft 365: sending a file in a Teams chat

An agent can send a file of its conversation or run into a Teams chat, 1:1 or group, as the account that authorized the integration: a report it exported as a PDF, a file a user attached. The tool is `send_microsoft_chat_file`, and the file is its `file_id`.

- The file is uploaded unchanged into the sending account's OneDrive, folder **Microsoft Teams Chat Files**, where Teams itself keeps chat files.
- Before the message is posted, every member of the chat except the sender is given read-only access to the file, signed in and without an invitation email. The file's permissions are then read back: each member must be listed with access.
- A grant that fails, or a member Microsoft does not list with access, returns an error naming the member and posts no message: nobody receives a file card they could not open. The file stays uploaded, and the error carries its link.
- On success the agent gets back the message id, where the file is stored and the members the read access was granted to.
- It is a write: a read-only binding of the integration does not offer it. The delegated permissions `ChatMember.ReadWrite` (to read the chat's members), `Chat.ReadWrite` and `Files.ReadWrite.All`, which the integration requests, cover the share and the post.

An agent account bound to Teams sends the files its answer names the same way. A `[[FILE:id]]` marker in the answer is taken out of the text and the file posted with the reply as a Teams file card:

- In a chat, the file goes to the account's **Microsoft Teams Chat Files** folder and is shared read-only with the chat's members, checked as above, before the reply is posted.
- In a channel thread, the file goes to the channel's Files folder, which the team's members already read.
- A file that cannot be uploaded, shared or carded, or a file past 100 MB, is not attached: the reply carries one plain line `File not attached: <name>` instead, never a raw marker. A marker naming a file of another user is dropped.
- A reply without a marker is posted as before.

The other markers of an answer become links, on Teams as on every account (Outlook, Gmail, WhatsApp): a deck, a report, a video or a dashboard (`[[PRESENTATION:id]]`, `[[REPORT:id]]`, `[[VIDEO:id]]`, `[[DASHBOARD:id]]`) is posted as a link to its page on XTM One, labelled with its title, and a deck marker that names a slide (`[[PRESENTATION:id#/slide]]`) opens the deck on that slide. A marker naming an item of another user is dropped, never posted raw; an investigation or a scheduled work item marker is removed.

Editing a message keeps its file. `update_microsoft_chat_message` and `update_microsoft_channel_message` send a message whole again, and Teams drops a file card the edit leaves out, so both tools read the message first and send its files back with the new text. The answer names the files kept. A card the message carried is replaced by the edit's own `card`, as before. A message that cannot be read is still edited, and the answer warns that a file it carried may no longer show. Files in a channel need no new grant: they sit in the channel's Files folder, which the team's members already read.

### Microsoft 365: people in a Teams group chat

An agent can see who is in a Teams chat and change the members of a group chat, as the account that authorized the integration. The chat keeps its thread: no new chat is created.

- **Who is in the chat.** `list_microsoft_chat_members` lists each member with their name, email, role and membership id.
- **Adding people.** `add_microsoft_chat_members` adds one or more people, by email address or Microsoft Entra user ID. By default they see the whole conversation since the chat was created; with `include_history` set to false they see only what is posted after they join. Someone already in the chat is left unchanged. The answer is the chat's member list after the change.
- **Removing someone.** `remove_microsoft_chat_member` removes one person, by email address, user ID or the membership id the listing gives. The answer is the member list after the change.
- A one-on-one chat cannot take new members (Microsoft Teams does not allow it): the agent is told so and nothing changes. Start a group chat with `create_microsoft_chat` instead.
- Adding and removing are writes: a read-only binding of the integration offers only the listing. A Teams channel Scope(s) removes all three tools, like the other chat tools.
- They need the delegated permission `ChatMember.ReadWrite`, which the integration now requests. On an integration authorized before, an administrator adds it to the app registration (API permissions, then consent) and the integration is re-authorized (`Re-authorize` on its page); until then Microsoft refuses and the answer names the permission.

### Microsoft 365: deleting Teams messages

An agent can delete a message it posted in Teams, as the account that authorized the integration: a post in a channel, a reply in a thread (`delete_microsoft_channel_message`, with the thread's first message as `parent_message_id` for a reply) or a message in a chat (`delete_microsoft_chat_message`).

- Teams shows the message as deleted, as when a person deletes their own message. Listing the channel or the chat afterwards shows it as `[message deleted]`.
- The agent gets back the message ID and the time Teams recorded the deletion. If reading the message back does not show it deleted yet, the result says so instead of reporting a deletion it could not confirm.
- Only the account's own messages can be deleted. If Microsoft refuses (another person's message, a message that does not exist, a missing permission), the agent gets the error as Microsoft returns it.
- Both are writes: a read-only binding of the integration does not offer them. A Teams channel Scope(s) limits the channel tool to the selected channels and removes the chat tool.
- They use the delegated permissions `ChannelMessage.ReadWrite` (channels) and `Chat.ReadWrite` (chats), which the integration already requests; editing a message uses the same ones. `ChannelMessage.ReadWrite` needs admin consent. If editing a channel message already works, deleting one needs no new consent. Otherwise, an administrator adds the permission to the delegated permissions of the integration's app registration in Microsoft Entra ID and grants admin consent, then the integration is re-authorized (`Re-authorize` on its page). `Chat.ReadWrite` needs no admin consent.

### Microsoft 365: reactions on Teams channel and chat messages

An agent can react with an emoji to a post or a thread reply in a Teams channel, as the account that authorized the integration, and remove that reaction (`add_microsoft_channel_message_reaction`, `remove_microsoft_channel_message_reaction`, with the thread's first message as `parent_message_id` for a reply). For example, it can mark an alert as already handled without anyone opening the thread. It can do the same on a message of a 1:1 or group chat it belongs to (`add_microsoft_chat_message_reaction`, `remove_microsoft_chat_message_reaction`); Microsoft's refusal for a chat the account is not in is returned as is. Both are write tools: an integration bound read-only does not offer them. An agent answering through a Teams agent account reacts as that account, and its turn knows the message it answers (`message_id`, with `chat_id` in a chat, or `team_id`, `channel_id` and, for a thread reply, `parent_message_id` in a channel), so it can acknowledge a message with an emoji instead of a reply.

- Any message of the channel can get a reaction, not only the account's own.
- The reaction is one emoji, in any skin tone, or one of the Teams reaction names `like`, `heart`, `laugh`, `surprised`, `sad`, `angry`, sent to Microsoft as its emoji. Anything else (text, a custom reaction) is refused before Microsoft is called.
- If Microsoft refuses (a message that does not exist, a missing permission), the agent gets the error as Microsoft returns it.
- Both are writes: a read-only binding of the integration does not offer them, and a Teams channel Scope(s) limits them to the selected channels.
- They use the delegated permission `ChannelMessage.Send`, which the integration already requests.

### Microsoft 365: mentions in Teams messages

An agent can @mention in the messages it posts in Teams, so the people it addresses are notified: an introduction to a channel, an agenda for the team, a question to one person. Typing `@channel` or a name in the text notifies nobody; the agent passes the mentions to the tool.

- **What can be mentioned.** In a channel message, a new post, a reply or an edit (`send_microsoft_channel_message`, `reply_microsoft_channel_message`, `update_microsoft_channel_message`): a person, the channel, the team or one of the team's tags. In a chat message (`send_microsoft_chat_message`, `update_microsoft_chat_message`): a person, or the whole chat (type `chat`, shown as @Everyone and notifying every member) in a group or meeting chat; a 1:1 chat refuses it. A chat mention names the chat the message is posted in, never another.
- **Which channel and team.** A channel mention names the channel the message is posted in, and a team mention the team of that channel. The account posts there, so it is a member; another channel or team is refused, so under a Teams channel Scope(s) a channel mention only ever names a selected channel.
- **Names.** The name shown in the message is the real name of the channel, the team, the person or the tag, read from Microsoft unless the agent gives one. A person is named by their email or Microsoft Entra object ID; a tag by its name or ID. Reading a team's tags needs the delegated permission `TeamworkTag.Read`, which the integration does not request: an administrator adds it to the app registration and grants admin consent, or the agent gives the tag's ID and name.
- **Where they go.** Each mention opens the message on a line of its own, unless the agent places it in the text.
- **What the agent gets back.** The result says which mentions Teams created; a mention Teams did not keep is reported as such. Listing a channel's messages shows each message's ID and its mentions (what was mentioned and its ID), so the agent can check a message it posted.
- **When Teams refuses.** A team's settings can turn off channel and team mentions. If Microsoft refuses the message, the agent gets the error as Microsoft returns it; the message is never posted again without its mentions.

### Microsoft 365: Adaptive Cards in Teams messages

An agent can post an Adaptive Card in Teams as the account that authorized the integration: a digest, a status summary or a set of links laid out as a card, in a chat (the account's own self-chat included) or a channel. The bot tools (`send_teams_bot_*_card`) post as the bot instead, and only where the bot is installed.

- **Which tools.** `send_microsoft_chat_message`, `update_microsoft_chat_message`, `send_microsoft_channel_message`, `reply_microsoft_channel_message` and `update_microsoft_channel_message` take a `card`: one Adaptive Card, as its JSON. The message text, when the agent gives one, shows above the card; a card can also be posted alone. Mentions work next to a card.
- **Width.** A card posted in a channel spans the full width, unless the card sets its own width. In a chat, Teams fits a card to the message bubble.
- **What a card may hold.** A message posted as a person has no bot behind it, so Teams runs only the `Action.OpenUrl` action. A card with inputs or any other action (`Action.Submit`, `Action.Execute`, `Action.ShowCard`, `Action.ToggleVisibility`) is refused before anything is posted; such a card goes through the bot tools. A message whose text and card together exceed a Teams message (about 28 KB) is refused too.
- **Editing.** An edit sends the whole message again: to keep a card, the agent passes it again.
- **What the agent gets back.** For a new post or a reply, the result says whether Teams kept the card, read from the message Teams returns; Teams returns no message for an edit, so the result says the card was sent. Listing the chat or the channel shows the card's text.
- **When Teams refuses.** If Microsoft refuses the message, the agent gets the error as Microsoft returns it; the message is never posted again without its card.

### Microsoft 365: folders shared in SharePoint and OneDrive

An agent can open a SharePoint or OneDrive folder shared with the account that authorized the integration from its link alone: the `Copy link` sharing link, or the folder's address in the browser. The folder does not need to be on a site the account is a member of, and no site has to be looked up first. The tool is `list_microsoft_folder_by_url`.

- **What the agent gets.** Every item of the folder with its kind (folder or file), its size, when it last changed, its web address and the URL the other file tools take. A file is read with `get_microsoft_file_by_url` or kept in the conversation with `download_microsoft_file`. A subfolder is listed the same way, or by adding its name to the folder's link (the link followed by `/Contracts`). A large folder is listed page by page, 50 items at a time by default and up to 200.
- **A folder given to the file tools.** `get_microsoft_file_by_url` answers a folder's link with the folder's first items and downloads nothing. `download_microsoft_file` refuses a folder.
- **Search.** Files newly shared with the account can take a while to show in `search_microsoft_files`; listing their folder shows them at once.
- **Sites.** `list_microsoft_site_files` takes a site by the ID `list_microsoft_sharepoint_sites` gives, by its address (`https://contoso.sharepoint.com/sites/finance`), or as `contoso.sharepoint.com:/sites/finance`. When the account cannot open the site, which is the case when all it holds there is a folder shared with it, the tool says the site is not accessible and points to `list_microsoft_folder_by_url`.
- It only reads: a read-only binding of the integration offers it. It uses the delegated permissions `Files.Read.All` and `Sites.Read.All`, which the integration already requests.

### Microsoft 365: SharePoint list item fields (metadata columns)

An agent can read the metadata columns a document library adds to its files (a choice, text, date or person column) with their values, without opening the files. It only reads: no tool writes a field.

- **One file.** `get_microsoft_list_item_fields` takes the file as the site plus its `Path` from `list_microsoft_site_files` (and the library when it is not the site's default one), or as the file's URL or sharing link. `columns` keeps only the named columns; a column the item does not carry is said so.
- **A whole library.** `list_microsoft_list_items` lists a list or a document library, named by its display name or list id, with each item's fields. `filter_column` and `filter_value` keep only the items whose column equals the value (case-insensitive, a multi-value column matches on any of its values); the filter is applied by the platform, so a column the library does not index works too. A listing with more items ends with a `page_token` to pass back for the next page.
- **Permissions.** Both use `Sites.Read.All`, delegated or application as the integration row is set up. A refused read answers an error naming that permission.

### Microsoft 365: commenting on a Word document

An agent can review a Word document in OneDrive or SharePoint the way a person does: it reads the document, then leaves native Word comments in it, as the account that authorized the integration. Each comment appears on the passage it is about, in Word desktop and Word for the web, under that account's name. The tool is `add_microsoft_document_comments`; the document is given by its link, or by its path (with the site for a SharePoint library).

- **Where a comment goes.** The agent quotes the exact passage the comment is on, as it read it. A passage that appears several times in the document needs its occurrence number (counted from the start of the document); without it, the agent is told how many times it appears and where. A passage the agent misquoted (a different case, typographic quotes, spacing) is refused with the document's exact text to quote instead. A passage that is not in the document is never commented anywhere else.
- **What changes.** Only the comments are added: the document's text, formatting and layout are left as they are, and the file gets one new version in its version history. The comments are the document's own comments, so people reply to them, resolve them and delete them in Word as usual.
- **What the agent gets back.** For every comment, whether it was placed (with the surrounding text) or refused and why. Comments that could not be placed are left out; the others are saved. If none can be placed, nothing is written.
- **When the document cannot be changed.** A document that is checked out, or open for editing by someone else (locked), is refused with nothing written: the agent says so and can try again once it is closed. If the account only has read access to the document, the tool refuses with a clear permission error. If someone saves a new version of the document while the comments are being prepared, nothing is written over it: the agent reads the document again and tries again.
- It works on `.docx` files up to 25 MB (not the older `.doc` format, PDF or other formats). It is a write: a read-only binding of the integration does not offer it. It uses the delegated permissions `Files.ReadWrite.All` and `Sites.ReadWrite.All`, which the integration requests.

The agent can also see the comments a document already holds, and take back comments it posted. The same documents, links and limits apply.

- **Listing the comments.** `list_microsoft_document_comments` reads the document and shows every comment with its number, its author, its date, the passage it is on and its text, and marks a comment that answers another one (`reply to #N`) or that was resolved in Word. The list covers comments made by anyone, in Word or by an agent. It is a read: a read-only binding of the integration offers it.
- **Deleting comments.** `delete_microsoft_document_comments` removes comments by their numbers from the list, or every comment by the connected account at once (`all_mine`). A deleted comment takes its replies with it, as deleting a thread in Word does. Only the comments are removed: the document's text, formatting and layout are left as they are, and the file gets one new version in its version history. Word shows the remaining comments on their passages as before.
- **Whose comments.** By default the agent can only delete comments made by the account that authorized the integration, under that account's name. A comment by anyone else is refused, and the agent is told so; it can remove other people's comments only when explicitly asked to include them (`include_others`). Word stores a comment's author as a plain name, so a comment by someone else under the same display name as that account counts as the account's own.
- **What the agent gets back.** For every number, whether the comment was removed (with its author, its passage and the replies removed with it) or refused and why: a number that is not in the document, a comment by someone else, or a comment placed in a footnote or an endnote (or with a reply there), which the agent cannot remove and which stays for you to delete in Word. If none can be removed, nothing is written. A checked-out, locked or read-only document, and a document someone saved meanwhile, are refused exactly as when adding comments: nothing is written, and the agent says so.

### Microsoft 365: formatting an email reply

A draft reply an agent writes in Outlook (`create_microsoft_draft_reply`, for a reply or a reply to all) stays in the conversation: Outlook shows it in the thread, above the quoted original message, like a reply you write yourself. The agent chooses how the text of the reply is laid out:

- **Plain text** (the default). The reply is written line by line, as typed. HTML or Markdown in it is shown as is, tags and symbols included. When a plain-text reply looks like HTML, the agent is told that its tags appear as text.
- **HTML**. Headings, tables, lists and links are rendered, as in an email the agent sends with `send_microsoft_email`, and cleaned the same way: scripts and unsafe links are removed.
- **Markdown**. Tables, bold text, headings and links written in Markdown are converted to HTML before the draft is saved.

The agent can also set the recipients of a reply: `to` replaces who it goes to, `cc` and `bcc` replace its Cc and Bcc (an empty value clears them). `send_microsoft_draft` takes the same three before it sends a draft.

### Microsoft 365: forwarding an email and replying in a thread held in another mailbox

An assistant who works for someone answers threads that exist only in that person's mailbox the way a human assistant does: the person forwards the message to the assistant's mailbox, and the assistant replies from her own mailbox to the original correspondents, with the whole thread quoted under her text.

- **Forwarding** (`forward_microsoft_email`). The agent forwards an email to one or more addresses, with an optional comment written above it. The forward carries the whole original: its text, its inline images and its attachments. The comment is signed like a reply. The forward also carries the thread the original belongs to, so the mailbox it reaches can answer in that thread.
- **Replying in the thread** (`reply_microsoft_email_in_thread`). On a message the mailbox holds, usually a forwarded copy, the agent writes a reply addressed to the participants of the original message, read from the header Outlook quotes in the forward: the original sender in To, the other participants in Cc. The sending mailbox is left out, and the person who forwarded the message stays in Cc only when the original included them. `to`, `cc` and `bcc` replace those recipients, for example to add a colleague in Cc. Outlook quotes the thread under the reply and the signature, as for any reply. The quote opens on the original message the forward carries: the forward's own header and note, an internal exchange, are left out with their inline images, so the correspondents never see them. `keep_forward_header` set to true keeps them.
- **How a reply looks.** Every Microsoft reply, draft and forward keeps the quote Outlook builds itself (`createReply`, `createReplyAll`, `createForward`), in HTML even when the message answered is plain text: the header block keeps one line per field and the original keeps its line breaks, whether the person sends the draft from Outlook or the agent sends it. The agent's text and the signature go above the quote. The text renders as an email: paragraphs, bold, links, and real bulleted or numbered lists, even when a list follows a line of text with no blank line. The signature's images are attached once: an image already on the message is reused and a second copy is removed.
- **A new subject.** `create_microsoft_draft_reply`, `reply_microsoft_email_in_thread` and `forward_microsoft_email` take an optional `subject` that replaces the `RE:` or `FW:` subject Outlook builds. The quote Outlook builds stays, and the signature stays directly under the text, above the quote: use it to quote an email under a new subject instead of pasting the quote into a new email, where the signature would land after it.
- **Which forwards are trusted.** The quoted header is text any sender can write in an email. The agent reads the original participants from it only when the forward was sent from the mailbox's own domain. A message with a forward subject from any other sender is answered to that sender, the agent is told the header was not trusted, and with `send` set the reply stays a draft unless `to` names the recipients the user confirmed. Such a reply never joins the thread the forward's headers name: it threads on the message the mailbox holds.
- **Draft or sent.** The reply is a draft by default, which the agent sends later with `send_microsoft_draft` or which the person sends from Outlook; sent that way, it threads on the forwarded copy, and the agent is told so. With `send` set, the agent sends it at once, once the user has given the go. A reply sent this way joins the original thread for its correspondents (its `In-Reply-To` and `References` name the original message, not the forwarded copy); the copy is kept in Sent Items. A reply to a message forwarded by another tool than `forward_microsoft_email` joins the thread when the forward kept its references, and the agent is told when it cannot.

### Platform email

The **Platform email** integration lets agents send email from the platform's own address, through the SMTP server the deployment configures (`SMTP_HOSTNAME` and the other `SMTP_*` variables), in the layout of the platform's own emails. It has no credential of its own.

- **Who creates it.** The catalog offers it only while SMTP is configured, and only to an administrator, who creates it company-managed (the switch is on and locked). The server refuses any other creation.
- **Allowed recipient domains.** Type a domain such as `example.com` and press Enter to add it; an entry that is not a domain name is marked and the form refuses to save (the API answers 422). Every address (to, cc, bcc, reply-to) must belong to one of them exactly; a subdomain is a different domain. With no domain listed, every send is refused. Through the API or an agent tool, the domains are one per line or a JSON list (`["example.com"]`); a list is stored one entry per line.
- **Daily limit.** The emails one agent may send in a UTC day: a whole number from 0 to 1000, prefilled with 50 (0 stops every send); a value outside the range is refused by the form and the API (422). The next send past the limit is refused until 00:00 UTC.
- **`send_email`.** `to`, `cc`, `bcc` (comma-separated), one `reply_to`, a one-line `subject`, a Markdown `body` sent as HTML, and `attachment_file_ids` (20 MB together). For an agent set to ask for tool approval, every call asks, whatever the whitelist or a standing approval says; an agent not set to ask, or a platform where an administrator suspended every approval ([AI governance](../../administration/ai-governance.md)), sends without asking, and the audit entry is the record. The same message to the same recipients twice is refused, and each bcc recipient gets their own copy so no header names them.
- **Audit.** Every send, delivered or refused by the SMTP server, writes one audit log entry (entity `platform_email`, action `send`) with the agent, the recipients, the subject and the outcome. The daily limit is counted from these entries.
- **Test Connection** says whether SMTP is configured, which domains are allowed and the daily limit.

### Email signatures (Letsignit)

An email an agent writes with an email integration (Microsoft 365 Outlook, Gmail, Amazon SES) can carry the person's signature, as an email that person writes does. The signature is read from a **signature integration** (Letsignit), by the sending address: the person's current signature and banner as Letsignit composes them now, and its reply variant for a reply when Letsignit defines one. The agent never writes a signature itself: the tools add it, and their descriptions tell the agent not to put one in the body.

**Where the signature goes.**

- **Outlook** (`send_microsoft_email`, `create_microsoft_draft_email`, `create_microsoft_draft_reply`, `send_microsoft_draft`): in the block Outlook writes, at the end of a new email and between a reply and its quoted thread. When the person opens a draft, the Letsignit add-in refreshes it there instead of adding a second one. A reply with no signature from Letsignit copies the signature of the account's recent sent mail.
- **Gmail** (`send_gmail`, `create_gmail_draft`, `reply_gmail`, `send_gmail_draft`): at the end of the HTML part, in the block Gmail writes for a signature, so a draft opened in Gmail shows it once. The plain-text part ends with its text version. An email the agent writes as plain text gains an HTML part to carry it.
- **Gmail bodies** (`send_gmail`, `create_gmail_draft`, `reply_gmail`): `body` is Markdown or plain text and is sent as a text / HTML alternative: lists, bold, links and paragraphs render in Gmail and Outlook, in Arial 14px black; a tag the text mentions shows as written. `body_html`, when given, is sent as the HTML part unchanged. `reply_gmail` takes `to`, `cc` and `bcc`, which replace the recipients the reply would get while keeping the thread (`threadId`, `In-Reply-To`, `References`). `get_gmail_message` returns the text part of a message once, or its HTML part as text when it has no text part.
- **Amazon SES** (`send_ses_email`): at the end of the HTML body and, as text, of the text body, for the `From` address. A templated or bulk send renders its stored template as it is, with no signature added.
- **Sending a draft** (`send_microsoft_draft`, `send_gmail_draft`): a draft that carries no signature yet gets one before it is sent; a draft that already has one is sent as it is, never signed a second time, with its attachments untouched.
- **Duplicate files on an Outlook draft** (`send_microsoft_draft`, and the forward and thread-reply sends built on it): a signature add-in can attach a draft's files again when the person opens it in Outlook. Before the send, a file attachment identical to an earlier one (same name, size and content) is removed and the answer says how many copies went; inline images are left alone, since Outlook drops the unreferenced ones itself. `get_microsoft_email` names the duplicate file attachments of a message or draft by name and count.
- **Signature images in Outlook.** Letsignit hands out a signature whose images (logo, banner, social icons) are links to its storage, which Outlook blocks in a received email. When the agent sends an email (`send_microsoft_email`, `send_microsoft_draft`, `forward_microsoft_email`, `reply_microsoft_email_in_thread` with `send`), each image of the signature is attached once to the email as an inline image, as the Letsignit add-in does when a person sends from Outlook, so the signature shows complete. A draft the agent creates (`create_microsoft_draft_email`, `create_microsoft_draft_reply`) carries the images inline too, each once: a person who opens it in Outlook and sends it sends one signature and one image set, and the agent sending it later adds nothing. An image that cannot be downloaded keeps its link.
- **Attachments once, body in the reader's theme.** Every file of `attachment_file_ids` is attached once, even when it is named twice or under two ids; after the files are attached the tool reads the message's attachments back, removes a second copy of a file (same name and size) and states the count in its answer. The markup the platform generates (a Markdown body's tables and links, the reply wrapper) carries no colour, background, font family or size, so Outlook shows it in the reader's theme (light text in dark mode); tables keep their borders and links their underline. An HTML body keeps the inline styles its author wrote (colour, background, font, border, padding, margin, width), a block with a margin of its own keeps it as written, and `<font>` tags and unsafe declarations (`url(...)`, `expression(...)`, `position`, `behavior`) are dropped.
- **Deleting a draft** (`delete_microsoft_draft`): deletes a message only when Outlook reports it as a draft; a received or sent email is refused and left untouched. The draft moves to Deleted Items and the answer names its subject and creation time.
- **A draft the person sends from Outlook** (`create_microsoft_draft_email` with `include_signature: false`): the draft carries no signature block and no signature image, so a signature add-in in the person's Outlook (such as Letsignit) inserts its signature exactly once when the draft is opened. A draft created with the signature (the default) can be signed again by such an add-in while it stays open, which adds inline images on every save; the tool's answer says so. Graph does not tell whether a client has a draft open, so the warning is given at creation. `send_microsoft_draft` still signs an unsigned draft when an agent sends it.
- **Reading a message's markup** (`get_microsoft_email` with `include_raw: true`): adds a window of the body's HTML (`raw_offset` pages through it), its signature blocks and add-in markers, its `cid:` references against the inline attachments, and every attachment with its content id and last modification time, oldest first. For diagnosing how a message or draft is built.
- **Reading a message's headers** (`get_microsoft_email` with `include_headers: true`): adds the message's internet headers (List-Unsubscribe, Precedence, Auto-Submitted, Received, Authentication-Results) as `name: value` lines, at most 60 headers and 300 characters per value, the cut said. For telling why a message reads as automated or where it came from; the default read neither asks Microsoft Graph for them nor shows them.
- **Reading a long message** (`get_microsoft_email` with `body_offset`): a body up to 20,000 characters of text is answered whole; a longer one by window, each window ending with `[Body truncated: N characters left; call get_microsoft_email again with body_offset=X ...]`, the last one with `[End of body]`. A body is never cut without saying so.
- **A quote written in the body.** When the text of an email the agent writes already contains an earlier message (an Outlook separator line, `-----Original Message-----`, or a `From:` line followed by `Sent:`, `Date:`, `To:` or `Subject:`), the signature goes between the agent's text and that quote, never under it.

**The setting of the email integration.** Every email integration has an **Email signature** card on its page (Integrations > the integration > Configuration):

- **Sign what its tools send**: on by default. Turned off, nothing its tools send is signed, not even an Outlook reply from the sent mail.
- **Signature integration**: the Letsignit integration the signature is read from. Left on **Automatic**, the platform picks one when an agent runs: the signature integration bound to the agent next to the email integration, else the only one the person the agent acts for may use. When there are several and none is chosen, or none at all, nothing is signed and the card says so.

**The override of an agent.** An agent bound to an email integration shows an **Email signatures** card in its settings, under its integrations, with one line per email integration it binds. Each line shows what the binding inherits (for example `Inherited: on, Letsignit`) and lets you turn signatures on or off for this agent only, or read them from another signature integration. Two agents bound to the same mailbox can sign differently, and two mailboxes bound to one agent each follow their own setting. The agent builder sets it too (`integration_signatures` on `create_agent` / `update_agent`; `get_agent` shows each override).

**Checking where it comes from.** `get_microsoft_email_signature`, `get_gmail_email_signature` and `get_ses_email_signature` show the signature an agent's emails carry and where it comes from: the signature integration (by name), the copy of the sent mail (Outlook replies), or none. When there is none, they say why and how to set one up.

- **Upgrading.** A deployment where a Letsignit integration is bound to an agent next to its Microsoft integration, and nothing is configured, signs exactly as before: signatures are on, and the bound Letsignit integration is picked.
- **Setting up Letsignit.** Create an API key with the Public API (`api`) scope in Letsignit (Settings > API keys), add a **Letsignit** integration with it, and click `Test connection`. The Letsignit tools also let an agent read a person's signature and read, create, update the variables of (job title, phone, address, custom variables) or delete a Letsignit user.
- **Changing a Letsignit user always asks a person.** The API key reaches every employee, and what an agent changes appears in every email that person sends afterwards. So every call that creates a Letsignit user, updates their variables or deletes one waits for a person to approve it, even when tool approvals are off or suspended, the tool is on the whitelist or you always allow it. A channel bot cannot collect that approval: it answers that it stopped. Reading a signature or a user runs without asking, like the other directory reads (see [Tools that always ask](../../administration/ai-governance.md#tools-that-always-ask)).
- **Signed by the mail server.** If your tenant signs mail on the server side (the Letsignit SMTP connector), every outgoing email is already signed in transit: turn **Sign what its tools send** off on the email integration, or emails sent by the agent are signed twice.

The mailbox of an Outlook agent account chooses its own signature source (see [Agent accounts](../../administration/channels/agent-accounts.md#mailboxes-microsoft-outlook)).

### Google: reviewing a Google Doc

An agent can review a Google Doc the way a person does, as the account that authorized the Google integration: everything it writes appears in the Doc under that account's name, with nothing prefixed.

- **Reading.** `read_google_doc` returns the Doc's text (every tab, with headings, lists and tables) and, apart, its open comment threads: who wrote each one, the passage it is attached to, its replies. Pending suggestions show inline as `{++inserted++}` and `{--deleted--}`. `list_google_doc_comments` lists every thread, resolved ones included on request.
- **Commenting.** `add_google_doc_comment` posts a comment attached to a passage, highlighted in the Doc like a comment made in the editor. The passage must occur exactly once in the Doc: otherwise nothing is posted, and the agent is told where the passage occurs or what the Doc's exact text is. Without a passage, the comment is on the whole file.
- **Replying and resolving.** `reply_google_doc_comment` answers a thread, and can resolve or reopen it; `resolve_google_doc_comment` resolves one, with an optional closing message.
- **Changing the text.** `edit_google_doc` replaces text, by default as suggestions the Doc's editors accept or reject. It changes the text directly only when asked to (`mode='direct'`, which needs edit access). Every replacement is counted first: a call whose text matches nothing, or more often than expected, changes nothing, and `dry_run` shows each match in context without changing anything. A direct edit is applied only to the version of the Doc it counted: if someone edits the Doc in between, nothing is applied.
- The four writing tools are writes: a read-only binding of the integration offers only the reading ones.

They use the OAuth scopes `https://www.googleapis.com/auth/drive` (commenting on a file the integration did not create) and `https://www.googleapis.com/auth/documents` (passages, suggestions and edits), which the integration requests. An integration authorized before it requested them shows a permissions change: re-authorize it (`Re-authorize with Google` on its page). Until then, a tool that needs a missing scope names it instead of calling Google. The Google Cloud project of the integration's OAuth client must also have the **Google Docs API** enabled; until it is, `read_google_doc` falls back on Drive's plain-text export, where `[a]`, `[b]`, ... mark the comments.

### Google: working in a Google Sheet

An agent keeps a Google Sheets spreadsheet up to date the way a person does, as the account that authorized the Google integration: an asset list, an exception tracker, a report it exports.

- **Finding it.** `search_google_sheets` finds a spreadsheet by name or content (an empty search lists the latest ones); `list_google_spreadsheet_sheets` lists its sheets (tabs), with their size and whether they are hidden. A spreadsheet is named by its ID or its URL.
- **Reading.** `get_google_sheet_values` reads a sheet, or a range of it in A1 notation (`A1:F200`, `A:C`, `2:10`), one line per row with its row number in the sheet. Given a column of the first row and a value, it returns only the rows that hold that value (case and spacing ignored).
- **Adding rows.** `append_google_sheet_rows` adds rows after the sheet's last row: as lists of values, or as objects keyed by the headers of row 1, each value placed under its header. A header the sheet does not have writes nothing.
- **Changing values.** `update_google_sheet_values` overwrites a range. `update_google_sheet_rows` finds rows by their value in a column of row 1 (an asset name, a ticket ID) and changes only the columns given; `append_or_update_google_sheet_rows` does the same and appends the rows it does not find. A key matched with its case is required: a key found in several rows, or (for `update_google_sheet_rows`) not found, writes nothing.
- **Removing.** `clear_google_sheet_values` empties a range or a whole sheet, keeping its formatting; `delete_google_sheet_rows` deletes rows by number, the rows below moving up.
- **Formulas.** Values are stored as given: a value starting with `=` stays text and is never run as a formula, since what an agent writes often comes from elsewhere. Pass `value_input_option='USER_ENTERED'` to have them parsed as if typed in the sheet (formulas, dates, percentages).
- Where tool approval is required ([AI governance](../../administration/ai-governance.md)), the three reading tools run without asking and the six writing tools ask first. A read-only binding of the integration offers only the reading ones.

They use the OAuth scope `https://www.googleapis.com/auth/spreadsheets`, which the integration requests, or the `https://www.googleapis.com/auth/drive` scope it already held: an integration authorized before shows a permissions change, and works without re-authorizing. The Google Cloud project of the integration's OAuth client must have the **Google Sheets API** enabled; until it is, the tools say so.

### Pennylane: ledger, invoices, bank transactions and analytics

An agent connected to Pennylane (Company API v2) reads and keeps the books the way an accountant does in Pennylane. One integration per Pennylane company: paste a company API token, or a firm token with the **Company ID** it should act on.

- **Ledger.** `search_pennylane_ledger_entry_lines` filters lines by date range, journal, ledger account (or an account number prefix such as `606`) and analytical category, with account and journal labels and page totals. `get_pennylane_trial_balance` sums every account over a period; `create_pennylane_export` starts a FEC, general ledger or analytical general ledger export that `get_pennylane_export` reads once ready. Ledger entries are created and updated only when balanced.
- **Invoices.** Customer and supplier invoices list by date, party, status, number or analytical category; `get_pennylane_invoice` gives the lines, payments and matched bank transactions, `download_pennylane_invoice_pdf` puts the PDF in the conversation. Customer invoices are created as drafts; finalizing, sending and the e-invoicing calls are their own tools.
- **Banking.** Bank transactions list with what they are matched to (`unmatched_only=true` for what is left to reconcile); matching, lettering and analytical categorisation are tools, as are bank accounts, bank establishments and SEPA, GoCardless and Pro account mandates.
- **Changes.** `list_pennylane_changes` reads Pennylane's changelogs (four weeks) for incremental reads.
- **Events.** An assignment can trigger on `supplier_invoice_created`, `supplier_invoice_updated`, `customer_invoice_created`, `customer_invoice_updated`, `customer_invoice_paid` (once per invoice), `transaction_created` and `transaction_matched` (again when the matched invoices change). They are polled from the changelogs every 5 minutes, starting from the moment the trigger is enabled, and each change is read with its record. Filter keys: `min_amount` and `max_amount` (euros, tax included), `supplier_id`, `customer_id`, `journal_id`, `bank_account_id`, `status` (the payment status of a supplier invoice) and `currency`; any other payload field (`invoice_number`, `label`...) is filtered with the usual operators.
- **Guardrails.** A tool is a read when its endpoint is a GET, else a write, so a read-only binding withholds every write. Marking an invoice paid, a supplier payment status, bank account and mandate writes, sending a document and deletions wait for a person's approval at each call. The tools that show or change bank data form one group (`BANK_DETAIL_TOOLS`) a deny list can withhold. A missing scope is named in the error; the token is never echoed.

### Ashby: candidates, applications, interviews and scorecards

An agent connected to Ashby works the recruiting pipeline the way a recruiter reads it in Ashby.

- **Finding people.** `search_ashby_candidates` finds a candidate by name, by email, or both. `get_ashby_candidate` gives the whole profile (contacts, current role, links, tags, source, custom fields, resume and files) and `list_ashby_candidate_applications` every job they were considered for.
- **Applications.** `list_ashby_applications` filters by status, job and creation date in Ashby itself, so listing everyone hired for a job (`status='Hired'`, `job_id=...`) never pages through archived applications. With `count_only=true` it answers how many applications match (for instance this week's, `created_after='-7d'`) with a breakdown by status and by job, reading every page itself (up to 5,000) instead of listing them. `get_ashby_application` gives the candidate, the job, the current stage, the source, the archive reason and the dates, including when the candidate was hired (Ashby records the moment they entered the hired stage). `list_ashby_application_history` gives each stage change: the stage left, the stage entered, when, how long it lasted and who moved it.
- **Scorecards.** `list_ashby_application_feedback` gives every scorecard on an application: who submitted it and when, the form, the interview and stage it was for, and every answer, the ratings and the selected options (such as the overall recommendation) included. `submit_ashby_application_feedback` checks the answers against the form before submitting: an option can be given by its label, a rating as a number from 1 to 4.
- **Interviews.** `list_ashby_interviews` lists the interviews booked for an application (or for everyone, or only those still to come) with their status, stage, times, interviewers and meeting link. `get_ashby_interview` opens one. `list_ashby_interview_config` gives a job's interview plan and its stages, the stage IDs needed to move a candidate. `schedule_ashby_interview` books an interview; `download_ashby_interview_transcript` keeps an AI Notetaker transcript in the conversation (Ashby add-on).
- **Jobs and offers.** Jobs come with their department, location and hiring team; postings and openings (headcount) can be read and managed. An offer is created in two steps: `start_ashby_offer` opens it and lists the offer form's fields, `create_ashby_offer` submits them.
- **Permissions.** Each Ashby API key holds read and write permissions per module. `Test connection` names the key and the permissions it holds (when the key has `API Keys Read`), and a tool the key may not use says which permission to grant. Without the key's `Allow access to confidential jobs and projects` setting, candidates of confidential jobs are invisible to the agent, as if they did not exist.
- A read-only binding of the integration offers only the reading tools.

### HiBob: who is writing to the agent

An agent with a HiBob integration bound knows who it is talking to without a tool call. In a chat, a Teams or Slack conversation or an email it answers, the agent receives a short **Sender directory profile** read from HiBob for the sender's email: name, job title, department and sub-department, site and time zone, manager, direct reports (named when there are 10 or fewer) and start date.

- **Read through the agent's binding.** The lookup uses the HiBob integration bound to the agent, so that integration's permissions apply.
- **Allow-listed fields only.** Only the fields above are requested from HiBob and shown: never compensation, performance, personal contact, health or equity, whatever the integration may read.
- **Not in the directory.** A sender HiBob does not know (a partner, a customer, a service account) is shown as not in the company directory.
- **Never in the way.** When HiBob does not answer in time, the profile says the directory is unavailable and the conversation goes on.
- **Read once a day.** The profile is kept for 24 hours, so the next messages of the conversation make no new HiBob request.
- **Turning it off.** Set `sender_directory_context` to `false` in the agent's configuration.

### DocuSign: sending a document for signature

A DocuSign integration lets an agent send a PDF or Word file of the conversation for e-signature and bring the signed copy back.

- **Two ways to connect.** *OAuth* (the default): create an app in DocuSign Admin > Apps and Keys, add the redirect URI shown in the setup guide, paste the Integration Key as Client ID and its Secret Key as Client Secret, then Authorize. *API Key* (the JWT Grant, for unattended assignments): generate an RSA keypair on the app, paste the private key in the API Key field, the Integration Key and your User ID in the extra fields, grant consent once with the link of the setup guide, then Test Connection. Pick the *Environment*: production, or demo for a developer account (test signatures only).
- **Sending.** `create_docusign_envelope` takes the `file_id` of one to ten PDF or Word files (attached, generated or downloaded in the conversation), a subject, a message, the signers (name, email, optional signing order) and optional carbon copies. With *anchor tabs* the signature, date or name fields are placed on a text of the document (for example `/sig1/`); without them the signer places the fields. The envelope is sent at once, or kept as a draft (`status: created`) to send later with `send_docusign_envelope`.
- **Following.** `get_docusign_envelope` reports the status (sent, delivered, completed, declined, voided) and every recipient's progress; `list_docusign_envelopes` lists what changed since a date; `resend_docusign_envelope` nudges the signers still pending; `void_docusign_envelope` cancels a sent envelope with a reason.
- **The signed copy.** `download_docusign_document` brings the signed PDF (every document combined, or one of them, the certificate of completion on request) into the conversation as a file you can download or forward to another tool. Like every download, it asks for approval unless the tool is whitelisted.
- **Templates.** `list_docusign_templates` and `create_docusign_envelope_from_template` fill a template's roles with named signers.
- **Read-only bindings** keep the status, listing, template and download tools; the sending, voiding and resending tools are withheld.
- **Real-time events with DocuSign Connect (optional).** Without it, envelope events arrive within 15 minutes. With it, within seconds. In DocuSign Admin > Integrations > Connect, add a custom configuration:
  - **URL to publish**: the integration's webhook URL, shown on the integration page (`https://<your platform>/api/hooks/integrations/<integration id>`).
  - **Data format**: REST v2.1 (JSON). **Include data**: Recipients (the envelope summary DocuSign sends must name the signers).
  - **Events**: Envelope Completed, Declined and Voided, and Recipient Completed (Recipient Signed in older accounts).
  - **Security**: enable *Include HMAC Signature*, add a Connect HMAC key, and paste it in the integration's *DocuSign Connect HMAC key* field. During a key rotation, store both keys separated by a comma.

  A delivery that no stored key signs is refused, and an integration with no key refuses every delivery. Each envelope change runs an assignment once, whether Connect or the 15-minute check reads it first. A delivery that cannot run when it arrives (AI turned off, the assignment's queue full) is left for the check.
- **Knowing when a document is signed.** An assignment with an *Integration event* trigger on DocuSign runs its agent when an envelope is completed (`envelope_completed`), declined (`envelope_declined`), voided (`envelope_voided`), or when one signer signs (`recipient_completed`). The platform checks DocuSign every 15 minutes and starts from the moment the trigger is enabled; each envelope change runs the assignment once, with the envelope id, its subject, the signers and who signed or declined. Filters narrow it to some envelopes (`envelope_ids`), signers (`signer_emails`, addresses or domains), senders (`sender_emails`) or subjects (`subject_contains`). A typical use: on `envelope_completed`, download the signed copy with `download_docusign_document` and send it on.

### NetSuite: finance across subsidiaries

A NetSuite integration gives the agents the ERP of a OneWorld (multi-subsidiary) company: reads, writes and the sensitive writes asked first.

- **Two ways to connect.** *OAuth 2.0 machine-to-machine* (recommended): an integration record with the Client Credentials grant, a certificate uploaded under Setup > Integration > OAuth 2.0 Client Credentials (M2M) Setup; paste the client ID and the certificate ID, and the certificate's private key in the API Key field. *Token-Based Authentication*: the consumer key and secret and the token ID, the token secret in the API Key field. A sandbox account ID such as `1234567_SB1` works as is. The setup guide lists every role permission, family by family; Test Connection lists the subsidiaries the role reaches.
- **Reading.** `run_netsuite_suiteql` runs any SuiteQL SELECT (paged, up to 5,000 rows); `describe_netsuite_records` lists the record types and their fields; `get_netsuite_record` and `list_netsuite_records` read any record. The finance tools answer with the currency, the subsidiary and the period of every amount: subsidiaries, accounting periods (closed, AP, AR and all locked), the chart of accounts, departments, classes, locations, currencies and exchange rates, customers, vendors, items, transactions by type with filters (subsidiary, period, account, entity, status, dates, minimum amount), one transaction with its accounting lines, the trial balance, income statement and balance sheet of one subsidiary or consolidated to it, AR and AP aging, intercompany balances by subsidiary and counterparty, bank lines, and the bank details of a vendor or customer.
- **Writing.** `create_netsuite_record` and `update_netsuite_record` change master data (customers, vendors, items, segments). Posting transactions (`create_netsuite_transaction` / `update_netsuite_transaction`: bills, invoices, credit memos, payments, journals, intercompany journals), `set_netsuite_period_status` (close, reopen, lock), `set_netsuite_entity_bank_details` and `call_netsuite_restlet` (except GET) ask a person on every call, whatever the agent's approval settings. Each family is its own tool, so a binding keeps only the ones it needs.
- **Saved searches, files and attachments.** `run_netsuite_saved_search`, `download_netsuite_file` (a file cabinet file or a transaction as PDF, into the conversation) and `attach_netsuite_file` go through a small companion RESTlet the administrator deploys once (Customization > Scripting > Scripts > New, a RESTlet deployed for the integration role; the RESTlets scope on the integration record and in the OAuth scopes field), then enters its script and deployment IDs on the integration:

```javascript
/**
 * @NApiVersion 2.1
 * @NScriptType Restlet
 */
define(['N/search', 'N/file', 'N/render', 'N/record', 'N/encode'], (search, file, render, record, encode) => {
  const post = (body) => {
    try {
      if (body.action === 'search') {
        const s = search.load({ id: body.id });
        const cols = s.columns.map((c) => c.label || c.name);
        const rows = [];
        s.run().getRange({ start: 0, end: Math.min(body.limit || 200, 1000) }).forEach((r) => {
          const row = {};
          s.columns.forEach((c, i) => { row[cols[i]] = r.getText(c) || r.getValue(c); });
          rows.push(row);
        });
        return { rows, more: rows.length >= Math.min(body.limit || 200, 1000) };
      }
      if (body.action === 'file' || body.action === 'pdf') {
        const f = body.action === 'file'
          ? file.load({ id: body.id })
          : render.transaction({ entityId: Number(body.id), printMode: render.PrintMode.PDF });
        const text = f.isText ? encode.convert({ string: f.getContents(), inputEncoding: encode.Encoding.UTF_8,
          outputEncoding: encode.Encoding.BASE_64 }) : f.getContents();
        return { name: f.name, type: f.fileType === 'PDF' ? 'application/pdf' : '', content: text };
      }
      if (body.action === 'attach') {
        record.attach({ record: { type: 'file', id: body.file }, to: { type: body.type, id: body.id } });
        return { ok: true };
      }
      return { error: `unknown action ${body.action}` };
    } catch (e) {
      return { error: e.message };
    }
  };
  return { post };
});
```

- **Events.** An assignment can start on NetSuite changes, polled every 5 minutes with SuiteQL (no script to deploy): *Vendor bill created or changed*, *Invoice created or changed* (invoices and credit memos), *Payment created or changed* (customer and vendor payments), *Journal entry created or changed* (journal entries and intercompany journals) and *Accounting period changed* (closed, reopened, locked or unlocked). Filters: `subsidiary_id`, `transaction_types`, `status`, `min_amount` and `change_kinds` (`created`, `updated`) on the transaction events, `states` on the period event. The first poll starts from now. The role needs SuiteAnalytics Workbook (SuiteQL) and view access to the transactions and periods it watches.
- **Limits.** NetSuite's concurrency limit (HTTP 429) is retried with backoff; a role missing a permission, or a transaction in a closed or locked period, is answered as such.

### HubSpot: activity, forms, sequences and the inbound report

An agent connected to HubSpot reads what happened to a record the way its activity tab shows it, from HubSpot alone.

- **Timeline.** `get_hubspot_record_timeline` gives a contact's, company's, deal's or ticket's activity in one call, oldest first (or newest first): every email (sender, recipients, subject, opens, sent manually or by a sequence), call (direction, outcome, duration), meeting (start, outcome, booked by the contact or not), note, task and logged LinkedIn, SMS or WhatsApp message, each with its time, owner and text; for a contact also its form submissions with the submitted values and its sequence enrollments; and the changes of lifecycle stage, owner and lead status (deal or ticket stage), with who made them. A date range and a list of types narrow it.
- **Engagements.** `get_hubspot_engagement` reads one email, note, call, meeting or task in full, with the records it is associated with; `search_hubspot_engagements` finds them by record, date range, text or property, for example every email to a contact between two dates. An engagement is never returned as a bare ID: associations to an engagement type, in `get_hubspot_associations` or a record read, are read and shown.
- **Forms.** `list_hubspot_forms`, `list_hubspot_form_submissions` (time, page, every submitted value, and the contact matched by the submitted email) and `get_hubspot_contact_form_submissions` (every form a contact submitted, not only the latest conversion).
- **Owners, teams, sequences.** Owners come with their teams; `list_hubspot_teams` lists each team's members. `list_hubspot_sequences` lists sequences and `get_hubspot_sequence_enrollments` a contact's enrollments, the emails each sent and the first reply. HubSpot does not expose the step a contact is on nor why it was unenrolled.
- **Labels and properties.** Lifecycle stages, lead statuses, pipeline stages, other options and owners are shown by their labels, the raw value alongside (`raw_values` shows only the raw value). The contact and company searches and reads take `properties` like the deal ones, and `list_hubspot_properties` lists an object type's properties and options.
- **Deal line items.** A deal's ACV and TCV are calculated by HubSpot from its line items. `list_hubspot_line_items` shows a deal's amount, ACV and TCV next to each line item (product, price, quantity, discount, amount, term, billing frequency, ACV, TCV) and their sum; `create_hubspot_line_item` (from a product or a name and a price), `update_hubspot_line_item` and `delete_hubspot_line_item` correct them. They need the `crm.objects.line_items.read` and `crm.objects.line_items.write` scopes and go with the deal object type in Scope(s).
- **Association labels.** `create_hubspot_association` adds a labelled association by label name or by category and type id, for example the Primary company of a deal, and `delete_hubspot_association` removes one label (the records stay associated) or the whole association. To move a deal's Primary company, add the label on the new company, then remove it from the old one; `get_hubspot_associations` shows the labels.
- **Many records at once.** `batch_get_hubspot_records` reads up to 500 records by ID and `batch_get_hubspot_associations` the associations of up to 1,000 records, 100 per request. Searches return up to 500 results per call.
- **Inbound report.** `get_hubspot_inbound_engagement_report` audits inbound follow-up in one call: every contact that submitted the named forms (or made any form conversion) in a date range, then per region (the owner's primary team, or a contact property), country, owner or form: submitted, assigned, touched, touched within 24 hours, first-touch delay, first-touch channel (manual email, sequence email, call, meeting), replied, meeting booked and held, deal created, and the contacts nobody has touched. It reads up to 2,000 contacts within four minutes and says what it could not read.
- **Scopes.** These tools need the private app scopes listed on the integration's setup guide: the engagement scopes (`sales-email-read`, `crm.objects.emails.read`, `crm.objects.notes.read`, `crm.objects.calls.read`, `crm.objects.meetings.read`, `crm.objects.tasks.read`), `files.ui_hidden.read` for engagement attachments, `forms`, `automation.sequences.read`, the `crm.schemas.*.read` scopes for labels and `settings.users.teams.read`. Each tool states the scopes it needs, and a tool that meets a missing one names the scope and itself: add it to the private app in HubSpot (the token does not change) and retry. A timeline or report missing one type still returns the rest.

### Mattermost: notifying a self-hosted chat

A Mattermost integration lets an agent post in the channels a SOC team watches and read what was said there.

- **Connecting.** Create a bot account (System Console > Integrations > Bot Accounts must allow them) or a personal access token for a dedicated user, add it to the teams and channels it posts in, paste the token in the API Key field and set *base_url* to your Mattermost URL.
- **Posting.** `post_mattermost_message` posts Markdown in a channel, or in a thread with the id of its first post; `post_mattermost_ephemeral_message` shows a message to one user only. `search_mattermost_channels` (with the team from `list_mattermost_teams`) gives the channel id.
- **Reading.** `list_mattermost_channel_posts` reads a channel's recent messages, newest first, with their post ids, so an agent can follow up in the right thread; reactions can be read, added and removed.
- **Channels and people.** Channels can be created, archived and restored, members added; users listed, looked up by email, invited to a team by email or deactivated.
- The token reads only the channels its account is a member of. A read-only binding keeps the reading tools; posting and every change ask for approval unless whitelisted.

### Kubernetes: a read-only view of a cluster's services

A Kubernetes integration lets an agent answer "what does this cluster expose" without a shell: the `kubectl get svc -A` read, and the node addresses a node port is reached on.

- **Connecting.** Create a service account bound to a ClusterRole that grants only `get` and `list` on `services`, `namespaces` and `nodes`, create a token for it, paste the token in the API Key field and set *base_url* to the API server URL (the `server` of your kubeconfig). When the API server's certificate is signed by the cluster's own CA, paste that CA (PEM) in *ca_cert*: the certificate is verified against it, never skipped.
- **Services.** `list_kubernetes_services` lists every service of the cluster, or of one namespace, with its type, cluster IP, ports with their node ports and target ports, external IPs and load balancer addresses; `service_type` keeps one type (for example `NodePort` or `LoadBalancer`).
- **Namespaces and nodes.** `list_kubernetes_namespaces` lists the namespaces; `list_kubernetes_nodes` lists the nodes, whether they are ready or cordoned, and their internal and external addresses.
- Every tool is a read: nothing is written to the cluster.

### Planhat: each person's own account and permissions

A Planhat integration connects in one of two ways, chosen with `Authentication mode` when you create it. Its `Setup guide` button shows the steps of the mode chosen: the OAuth Client in Planhat's App Center for OAuth, the Private App for an API key.

- **OAuth: each person's own account.** A Planhat admin opens `App Center > New App > OAuth Client` in Planhat, sets its Redirect URL to the callback shown in the setup guide (`<your XTM One address>/api/oauth/callback`), and grants the data models the agents use. Paste the Client ID (`pk_...`) and the Client Secret (`sk_...`) in the integration, save, and click `Authorize`. The sign-in is Planhat's own, so it goes through your company's SSO when Planhat is set up with one.
- **What the agent can do.** With OAuth, the agent reads and writes with the permissions both the OAuth Client and the person's Planhat role allow: a record the person cannot see in Planhat, the agent cannot see either.
- **Everyone with their own account.** Share the integration (or make it company-managed) with `Don't share my connected account` on: each person then clicks `Connect your account` on the integration's page and the agent acts as them (see [Shared without the connected account](#shared-without-the-connected-account-each-person-authorizes-their-own)).
- **How long a connection lasts.** The access token is renewed every hour, automatically. The renewal itself expires after a year: the person then authorizes again.
- **API key instead.** A Planhat Private App's API Access Token: one technical account for everyone, with the Private App's permissions whatever the person, and every action recorded in Planhat as the Private App's.

### Zendesk: remediation and incident tickets

A Zendesk integration lets an agent open and follow tickets in the help desk the remediation teams already work from.

- **Connecting.** In Admin Center > Apps and integrations > APIs > Zendesk API, enable token access and add an API token. Paste it in the API Key field, set *subdomain* (`acme` for `https://acme.zendesk.com`) and *email*, the agent the token acts as: a dedicated agent seat makes the agent's changes easy to tell apart.
- **Tickets.** `search_zendesk_tickets` filters by status, tags and text (Zendesk search terms such as `priority:urgent` work too); `get_zendesk_ticket` gives the description and the comments. `create_zendesk_ticket` opens one with a priority, a type, tags, a requester, an assignee or group, and an *external id* tying it to the finding it tracks.
- **Updating.** `update_zendesk_ticket` changes the status, priority, assignee or tags and adds a comment. A comment is an internal note unless the agent asks for a public reply, so the requester is only written to on purpose. A deleted ticket can be restored for 30 days.
- **People.** Users and organizations can be searched, read, created, updated and deleted; `list_zendesk_ticket_fields` shows the account's custom fields.
- A read-only binding keeps the reading tools; every change asks for approval unless whitelisted.

### Jira: mentioning a person in a comment

An agent can mention a person in the Jira text it writes (a comment, an edited comment, a description, a worklog comment), so Jira notifies them. Typing `@Name` notifies nobody; the agent writes `[~accountid:ID]`, the Jira wiki syntax, with the person's account ID, which `search_jira_users` returns. Each token becomes a mention showing the person's name; text without a token is written as before.

### GitHub: files in an issue or pull request comment

GitHub offers no upload API for comments, so an agent attaches a file of the conversation (a capture, a report, an archive) through the repository itself.

- **Pull request bodies too.** `create_github_pr` and `update_github_pr` take the same `attachment_file_ids`, for a before / after capture in the description. The files go to the same `xtm-assets` branch (under `xtm-assets/pr-<head branch>/` when the pull request is created, `xtm-assets/<number>/` when it is updated), never to the pull request's own branch, so they are not in its diff. `update_github_pr` appends the lines to the new body, or to the current one when no `body` or `edits` is given.

- **How.** `add_github_comment` and `create_github_pr_review` take `attachment_file_ids`: the files are committed, in one commit GitHub signs, to the branch `xtm-assets` of the same repository (created from the default branch the first time; the default branch is never written), under `xtm-assets/<number>/`. The comment gains one line per file: a `png`, `jpg`, `gif` or `webp` shows inline, a `pdf`, `zip`, `txt`, `csv`, `json`, `md`, `log`, `html` or `svg` is a link. The links point at that commit, so they never change.
- **Limits.** At most 10 files and 5 MB in all per comment. A file missing, of another type or over the cap refuses the call before anything is written: no commit, no comment.
- **Who sees them.** Anyone who can read the repository; in a private repository the files stay private.

### GitHub: a comment never posted twice

An agent that posts a verbatim comment (a review verdict, a status line) from a run that may be retried passes `dedupe_first_line: true` to `add_github_comment`. The comments already on the issue or pull request are read on the server: when one opens with the same first non-empty line and holds the same text as the new comment, nothing is committed or posted. A comment that shares only the first line (a verdict header reused for a new round or a new head) does not stop the post. When the post is skipped, the answer names that comment (author, date, link). The earlier comment bodies never reach the agent. Without the option the tool posts as before.

### GitHub: a comment corrected in place

`update_github_comment` changes a comment the account can edit, named by its id or by its link (`...#issuecomment-N`). `edits` are find/replace edits on the comment as GitHub stores it: every find must exist and be unique (or set `replace_all`), nothing is written unless every edit applies, and the answer is the diff, not the comment. `body` replaces the whole comment instead. `expect_updated_at` (the `updated_at` the agent read) refuses the write when someone changed the comment since. A wrong figure in a posted verdict is then fixed in the same comment, never by a second one.

### GitHub: labels set when the pull request opens

Some repositories run a check on a new pull request that reads its labels at start (a `dependencies` label that exempts a bump from the linked-issue rule, for example). A label added in a later call can arrive after that check, which then fails. `create_github_pr` takes `labels`: each one is checked on the repository before anything is written (an unknown label refuses the call and is named), then the labels are written first after the pull request is created, before the assignees, and the result reads them back on a `Labels:` line. Without `labels` the tool behaves as before. `create_github_pr` also takes `milestone` (a number or the exact title of an existing milestone; an unknown one refuses the call, and a milestone is never created). `create_github_issue` and `update_github_issue` take `milestone` the same way. Without it, the pull request inherits the milestone of the first issue of the same repository it closes (`Closes #N`), and the `bug` or `feature` label of the closing issues is added to its labels; the result names the milestone on a `Milestone:` line, or says the closing issue has none.

### GitHub: a pull request taken out of the merge queue

A pull request waiting in a merge queue cannot take a new commit without being ejected. `manage_github_pr` with `action: dequeue` takes it out first: it reads the pull request's merge queue entry, answers that nothing was written when the pull request is not queued, and otherwise removes it and names the position it held. After the push, `merge_github_pr` adds it to the queue again.

### GitLab: remediation issues, merge requests and files

A GitLab integration lets an agent turn an exposure into a ticket the development team works from, and follow it to the fix.

- **Connecting.** Create an access token with the `api` scope (`read_api` for read-only use): a project or group access token for a service account, or a personal one. Paste it in the API Key field. For a self-managed GitLab, set *base_url* to its URL; leave it empty for GitLab.com.
- **Issues.** `list_gitlab_issues` filters a project's issues by state, labels, text and assignee; `create_gitlab_issue` opens one (labels, assignees, due date, confidential); `update_gitlab_issue` closes or reopens it, adds or removes labels, reassigns it or locks its discussion; `add_gitlab_issue_note` comments. A project is named by its id or its `group/project` path.
- **Merge requests.** List, read and comment on merge requests, for example to follow the fix of a remediation ticket.
- **Repository files.** `list_gitlab_repository_tree` and `get_gitlab_file` read a dependency manifest or a configuration; `update_gitlab_file`, `create_gitlab_file` and `delete_gitlab_file` commit directly on the branch given, so use a dedicated branch and a merge request for review.
- **Releases.** List, read, create, edit and delete releases.
- A read-only binding keeps the reading tools; every change asks for approval unless whitelisted.

### OpenCTI: reading a managed connector's logs

For a connector OpenCTI deployed through its connector manager (XTM Composer), `manage_opencti_connector` with `action="logs"` and a `connector_id` returns the latest lines the manager reported, the ones the connector page of OpenCTI shows: `tail` lines (50 by default, at most 500), oldest first, under a line naming the connector, its status and its uptime. Long lines are cut, the answer stays under 30,000 characters (the oldest lines go first) and credential-shaped values are masked. `action="get"` also shows the last 5 lines. A connector OpenCTI does not manage has no logs there, and an OpenCTI version without managed connector logs is named as such.

### OpenCTI: resetting a connector's state

`manage_opencti_connector` with `action="reset_state"` and a `connector_id` does what the Reset state button of the connector page does: OpenCTI clears the connector's stored state and purges its queues, so it ingests again from the beginning. The answer names the messages that were waiting before the reset and what happens next: a running connector starts over on its next run, a stopped managed connector needs `action="start"`. It needs the `MODULES_MODMANAGE` capability, and on a binding without live writes it asks for approval like every other change.

### PagerDuty: paging on-call responders

A PagerDuty integration lets an agent page the people on call when an exposure cannot wait, and follow the incident to its resolution.

- **Connecting.** Create a REST API key in PagerDuty (Integrations > API Access Keys, or a user token), paste it in the API Key field, set *region* to `eu` for an EU-hosted account, and set *from_email* to the PagerDuty user the changes are recorded as: PagerDuty requires it on every incident change, and without it only the reading tools work.
- **Paging.** `list_pagerduty_services` gives the service to page, `create_pagerduty_incident` opens the incident (title, details, urgency, an escalation policy or assignees). An *incident key* de-duplicates: a scheduled assignment that finds the same exposure again adds nothing while the first incident is open.
- **Following.** `list_pagerduty_incidents` filters by status, urgency, service and dates; `get_pagerduty_incident` gives the details and the assignees; `list_pagerduty_log_entries` the notifications, acknowledgements and escalations. `update_pagerduty_incident` acknowledges, resolves (with a resolution note), escalates or reassigns, and `add_pagerduty_incident_note` records progress.
- **Who is on call.** `list_pagerduty_oncalls` names the responders on call now per escalation level, `get_pagerduty_user` their contact details.
- A read-only binding keeps the reading tools; opening, changing and annotating incidents asks for approval unless whitelisted.

### GIPHY: GIFs in a conversation

A GIPHY integration lets a conversational agent answer with a GIF or a sticker when you ask it to, for example to celebrate a resolved incident.

- **Connecting.** Create an API key in the GIPHY Developers dashboard (choose API, not SDK), paste it in the API Key field and run Test Connection. A new key is a beta key that GIPHY limits to a few calls per hour: apply for a production key from the same dashboard once the agent is in use.
- **Telling the agent when.** Bind the integration to the agent and say in its instructions when a GIF fits, for instance "end a resolved incident with a celebration GIF" or "only use a GIF when the user asks for one". Without one, the agent judges by itself when a GIF fits.
- **What the agent can do.** Search GIFs or stickers by words, ask for the single best GIF for a phrase or a feeling, draw a random GIF for a tag, or list the trending ones.
- **Content rating.** Every search asks GIPHY for GIFs rated G by default; the agent can go up to PG-13 when its instructions ask for it, never to R.
- **Where it shows.** The GIF appears in the web chat, and as an image in Microsoft Teams, Slack, Telegram, Discord and Google Chat. WhatsApp takes only JPEG and PNG pictures, so a GIF reaches it as a link.
- **A GIF library of your own.** A knowledge base can hold your favourite GIFs as a list of GIPHY links with when to use each one; an agent that reads it can answer with one of them, shown the same way, even without the integration.

### SecurityScorecard: ratings, factors and findings

A SecurityScorecard integration shows an agent how an organisation looks from the outside, its own or a vendor's.

- **Connecting.** Generate an API token in SecurityScorecard (My Settings > API, or a bot user for unattended assignments), paste it in the API Key field and run Test Connection.
- **Rating and its causes.** `get_securityscorecard_company` gives a domain's score, grade and 30-day change. `list_securityscorecard_factors` lists the ten factors (patching cadence, network security, DNS health...) with the issue types each one counts, and `list_securityscorecard_issues` the findings of one issue type: the exposed hosts, IPs, ports, products and CVEs, with when each was first and last seen. Score and factor history and an industry's average score put the rating in context.
- **Vendors.** `list_securityscorecard_portfolios` and `list_securityscorecard_portfolio_companies` list the third parties you follow with their ratings; companies can be added to or removed from a portfolio, and portfolios created or deleted.
- **Reports.** `generate_securityscorecard_report` asks for a report (summary, issues, full JSON...), built in the background; `list_securityscorecard_reports` gives its download link once it is ready.
- A read-only binding keeps the reading tools; portfolio changes and report requests ask for approval unless whitelisted.

### WhatsApp

The `WhatsApp` integration operates one business phone number on Meta's WhatsApp Business Platform (Cloud API). An agent bound to it sends text, approved templates, media, and interactive messages to a number or to a group the business created, reacts, marks messages read, downloads received media, and reads the number's profile and groups. It is the integration counterpart of the [WhatsApp channel bot](../../administration/channels/whatsapp.md): the bot lets people talk to an agent, the integration gives an agent a number to work with.

**Set it up in Meta.**

1. In Meta for Developers, create a Business app and add the `WhatsApp` product.
2. Register a business phone number dedicated to the agent. A number already used in the consumer WhatsApp app cannot be operated by the API.
3. In Meta Business Suite > `Settings` > `System users`, create a system user, assign it the app and the WhatsApp Business Account, and generate a permanent token with `whatsapp_business_messaging` and `whatsapp_business_management`.

**Create the integration.** In `Tools & Integrations > Integrations`, add `WhatsApp` and fill in:

| Field | What it holds |
| --- | --- |
| `API Key` | The system user's permanent token (stored encrypted) |
| `phone_number_id` | The Phone number ID from WhatsApp Manager > `API Setup` (required) |
| `waba_id` | The WhatsApp Business Account ID (optional, for template and number management) |
| `app_secret` | The Meta app secret, which verifies the signature of inbound notifications (needed to receive messages safely) |
| `verify_token` | A token you choose and also enter in the Meta app's webhook configuration |
| `inbound_retention_days` | How many days received messages are kept for the agent to read (30 by default, 1 to 365) |

`Test connection` reads the number's profile with the token.

**Receive messages.** The `Webhooks` card of the integration's `Configuration` tab shows the callback URL, ending with `/api/channels/whatsapp/webhook`, with a copy button. In the Meta app, go to `WhatsApp` > `Configuration` > `Webhook`, paste the URL and the `verify_token`, and subscribe to `messages`. The same URL serves the WhatsApp channel bot: each notification goes to the integration whose `phone_number_id` it names, and any other number reaches the bot as before. Received messages are kept for `inbound_retention_days` and read by the agent with `list_whatsapp_conversations` and `read_whatsapp_messages`.

**Known limits of the Cloud API.**

- It operates a business phone number, never a personal WhatsApp account.
- Groups need an Official Business Account (business verification). The business creates the group and members join with an invite link; a business number cannot be added to an existing group. A group holds at most 8 participants.
- Interactive messages (reply buttons, lists) work in one-to-one conversations only.
- Polls can be neither created nor read: a member's poll arrives without its question, options, or votes.
- Free-form messages need an open 24-hour customer service window; outside it only approved templates go out.

### Activity

`Activity` is the change history — when the integration was edited, whether it was tested recently, and whether an issue started after a configuration change.

## OpenAPI and Swagger APIs

The `OpenAPI / Swagger` provider connects any REST API that publishes a Swagger 2.0 or OpenAPI 3.x document, in JSON or YAML. No code is generated: every operation you select becomes one agent tool, and each call is built from the document — the path, query, header, cookie and body parameters, serialized the way the spec declares them.

To create one, choose `OpenAPI / Swagger` in the catalog, then paste the URL of the raw document (`swagger.json`, `openapi.yaml` — not the Swagger UI page) or upload the file, and click `Load spec`. A spec split across several files (`$ref: schemas/pet.yaml`) is imported from its URL, where each referenced file is downloaded too, or uploaded as a ZIP archive of all its files. The preview then asks for:

- **The API base URL**, pre-filled from the spec's servers. A spec whose server is relative can only fill it in when it was loaded from a URL.
- **A tool name prefix**, derived from the API's title, so agents can tell this API's tools apart from others.
- **The authentication**, from the security schemes the spec declares: an API key (in a header, the query string or a cookie), a bearer token, HTTP Basic, OAuth2 (authorization code, implicit, device code, client credentials or password), OpenID Connect, a client certificate (mutual TLS), or none. The API key is optional: without one, calls are sent without a credential, and you can add the key later in `Configuration`.
    - For the authorization code and implicit flows, register `<platform URL>/api/oauth/callback` as the redirect URI in the API's OAuth app, create the integration, then click `Authorize`. An implicit token cannot be refreshed: click `Authorize` again when it expires.
    - With a device code, `Authorize` shows a code and a link: open the API's sign-in page, enter the code and approve. No redirect URI is needed.
    - Client-credentials and password tokens are requested when tools run and reused until they expire. The password flow takes the username and password of the account the API should act as; its client ID and secret are only needed when the API asks for them.
    - An OpenID Connect scheme is read from its discovery document when the spec loads, and offers the flows its provider supports; its sign-in requests the `openid` scope, plus `offline_access` when the provider supports it.
    - A client certificate can be added to any option, under `Client certificate`: paste or load the PEM certificate, its private key, and the key's passphrase when it is encrypted. It is sent on every call and every token request. An expired certificate is refused, and calls stop with a message saying so; from 30 days before its expiry, the `API Spec` tab and the integration's overview flag it. To rotate it, have the new certificate issued before the old one expires, then click `Replace` under `Client certificate` in the `API Spec` tab: the next call presents the new one.
- **The operations to expose**, grouped by tag. Deprecated operations start switched off, and `Read only` keeps only the operations that read (GET, HEAD, OPTIONS and QUERY). The pencil next to an operation renames its tool or rewrites its description; agents choose tools by these, so a clear name and description help.

The name and description of the integration can be set on the same page. The description is filled from the spec, and agents read it as context for every tool of the API.

Operations that upload a file become tools too: a multipart file field, or a request body in a file format such as `application/octet-stream`, an image, a PDF or XML. The agent passes a file available in the conversation, such as a file the user attached, or text it writes, such as a CSV it builds. These operations carry a `File` badge in the list.

Every HTTP method becomes a tool, including HEAD and OPTIONS, which answer with the response status and headers, and the OpenAPI 3.2 QUERY method and custom methods. TRACE and CONNECT never do: TRACE echoes the request back, credentials included, and CONNECT opens a tunnel instead of calling the API. OpenAPI 3.2 documents are read with their additions too: a `querystring` parameter is one argument that holds the whole query string, JSON Lines bodies take a list of items, a `$self` URL is the base of the document's relative links, and tag summaries name the groups of operations.

Some operations cannot become tools, and the preview says why for each: file content in a query, header or path parameter or in a URL-encoded form, TRACE and CONNECT, and definitions that cannot be resolved (a referenced file that cannot be downloaded, for example). An operation the spec marks as public (`security: []`) is called without the credential. So is an operation the spec places on another server than the base URL (another host, scheme or port), so that a spec cannot send the credential elsewhere; the preview flags these operations.

Calls can reach public hosts and private-network hosts, so an intranet API works. Loopback, link-local and cloud-metadata addresses are always refused, for the spec download, the files it references, and every tool call and token request. Each connection goes to the address that was checked, so a host name that resolves differently a moment later cannot redirect it. Behind an HTTP proxy, which makes the connections itself, each request's URL is checked before it is sent.

An OpenAPI integration adds an `API Spec` tab to its detail page. It shows where the spec came from, its version and authentication, and lets you change which operations are tools, and their names and descriptions, without importing again. `Refresh from URL` downloads a URL-sourced spec again: selected operations that disappeared are dropped, new ones stay off until you select them, and the names and descriptions you set are kept. `Replace spec` loads a different URL, file or archive; the new document must offer an authentication that uses the same kind of credential. An OpenAPI integration that holds no spec yet (one created before spec import existed) gets its first spec the same way, through `PUT /api/v1/integrations/{id}/openapi` or the agent builder's `update_integration` with `openapi`: its stored credential is kept, and the spec's absolute server becomes the base URL when none is set. The OAuth endpoints and scopes follow the chosen option: the ones it changes are updated, the others keep your edits. The tab also shows the OAuth endpoints and scopes, which you can edit, and the client certificate: its subject, issuer and expiry, with `Replace` and `Remove`. `Test connection` calls one enabled GET operation that needs no argument.

Agents bind an OpenAPI integration as that specific integration. There is no per-user binding for it, because each OpenAPI integration is a different API. For the members of a shared agent to use its tools, share the integration with their groups.

Admins can switch the whole provider off from the integration catalog, like any other provider.

## Several accounts of one provider on an agent

When an agent binds two or more integrations of the same provider (two Microsoft accounts, for example), each account's tools get a suffix so the agent can tell them apart: `list_microsoft_calendar_events__corporate` and `list_microsoft_calendar_events__personal`. The suffix is the integration's **tool suffix** (`tool_slug` in the API). It is derived once from the integration's name when the integration is created and does not change when the integration is renamed, so the agents, skills, prompts and assignment gates that name a suffixed tool keep working after a rename.

To change it, set `tool_slug` through the integrations API (`PUT /api/v1/integrations/{id}`) or the agent builder's `update_integration` tool (`create_integration` takes it too): lowercase letters, digits and underscores, at most 21 characters, unique among the owner's integrations of that provider. An invalid value is refused before anything is written, and a suffix another integration of the same provider and owner holds is refused with the reason. The new name is used from the next run; anything that names the old suffixed tool must be updated. The agent builder's `list_integrations` and `get_integration` tools show each integration's suffix, and `get_agent` shows the suffix of each binding.

An assignment's tool gate (`activation_check`) can name one account's tool without its suffix, by its base name and the integration id: `"tool": "list_microsoft_calendar_events@<integration id>"`, or `"tool": "list_microsoft_calendar_events"` with an `"integration_id"` key. The current suffixed name is found when the gate runs, so the gate survives any change of the suffix.

## Read-only and managed nuances

Integrations can have more nuanced edit behavior than other resources. Company-managed integrations are read-only for non-admins, and some suite-level registrations behave like centrally managed records rather than personal connections. Status, tools, and related usage stay visible even when editing is restricted.

## Sharing an OAuth integration

An OAuth integration is connected with one person's account: whoever authorized it. You can share it like any other integration - with groups, with individual users, or company-wide by making it company-managed - and once it is shared, **everyone it is shared with acts using that connected account**. Their agents, assignments and flows use the owner's token, so actions in the external service appear as the owner.

That is the default. When sharing is turned on in `Configuration`, a notice says so, with one switch below it:

- **`Don't share my connected account`** (off by default; an admin editing someone else's integration sees `Don't share the owner's connected account`) keeps the integration shared without the owner's account: the owner keeps their connection and is the only one who uses it, and **everyone else it is shared with authorizes the integration with their own account** (see below). The choice is kept: authorizing the integration again never shares the owner's account. Only the owner or an admin can turn it on or off.
- Reconnecting a shared integration that shares its account shares the account used to reconnect it; the `Authorize` button says so. A group-shared or user-shared integration is reconnected by its owner. A company-managed integration can be reconnected by any admin, who then becomes its owner. Turning `Don't share my connected account` off again shares the owner's account and disconnects everyone's own.
- Saving asks once: a confirmation names whose account it is, who will act with it and how to avoid it. Confirming shares it; cancelling changes nothing.
- A member who edits a shared integration without being its owner or an admin cannot share the owner's account: if the owner never shared it, saving a new share shares the integration without it, and the confirmation says so before anything is saved.
- Stopping sharing - removing every group and user and turning company-managed off - makes the integration personal again: only its owner uses the account until it is shared again.

In a chat, people who cannot see the integration, or whose owner's connected account is not shared with them, use their own account of the same provider instead when they have one; when they do not, a note under the answer says the integration was not used for them and why, and the agent is told too. What carries it - an agent's runs, public link and channels, a flow, a knowledge base import source, a skill's source - acts with its account for that thing's audience, as the next section explains.

The dashboard reminds owners which of their integrations share their connected account, with a shortcut to stop sharing.

Through the API, the choice is the `keep_oauth_credentials_on_share` field of `POST /api/v1/integrations` and `PUT /api/v1/integrations/{id}`: `true` shares the connected account, `false` shares the integration without it. A request that shares the integration without the field keeps the account private, as it always did: an account whose sharing was never consented to stays its owner's, and everyone else authorizes their own. An edit that does not change sharing and sends no choice changes nothing.

An agent makes the same choice with the `share_connected_account` argument of the `create_integration` and `update_integration` tools (`false` is `Don't share my connected account`; `credential_mode` `per_user` or `shared` says the same). It is written exactly as the switch writes it, and `true` is refused for someone who is not the owner, the account's holder or an admin. `get_integration` and `list_integrations` report the mode of an OAuth integration and, when it is `per_user`, how many people connected their own account. The mode protects the other people's chats: the owner's own agents keep acting with the owner's account in their assignments and flows.

### Shared without the connected account: each person authorizes their own

On an OAuth integration shared with `Don't share my connected account`:

- **Everyone it is shared with** sees a `Your account` block on the integration's page: `Connect your account` opens the provider's sign-in for their own account (the integration's OAuth app, the same one for everyone), with its status and `Disconnect my account`. The integrations list says whether their account is connected.
- **Every run acts with the account of the person it runs for**: a chat, the person chatting; an assignment, the person it runs as (else the agent's owner); a flow, its owner; the public widget, event polling and notifications, the agent's owner. Never with someone else's: someone who has not connected theirs gets no tools from the integration, and a note under the answer links to the page where they connect it.
- **The owner** connects first and keeps their own connection; the integration reads as connected when theirs is. The people who manage it see who connected an account (names and status, never the account) and can disconnect anyone.
- A new OAuth app (client ID) disconnects everyone's own account, and someone who can no longer see the integration loses theirs. A skill's private source cannot use such an integration: a skill is shared and refreshed unattended, with no one person whose account it would act with.

This works for every OAuth integration, OpenAPI ones included. Through the API, the choice stays `keep_oauth_credentials_on_share` (`false` shares the integration without the account); the integration reports it as `credential_mode` (`shared` or `per_user`). A person disconnects their own account with `DELETE /api/v1/integrations/{id}/my-credential`; the managers list the connected people with `GET /api/v1/integrations/{id}/credentials` and disconnect one with `DELETE /api/v1/integrations/{id}/credentials/{user_id}`.

## Lending your integrations through a shared agent

An agent you share with groups or people can be bound to your personal integrations. You choose, per agent, what the people it is shared with get when they chat with it:

- **Use my integrations through this agent.** The agent runs with your connected accounts in their chats too, under its persona, guardrails and tool scoping. The integration stays yours: it is not listed on their Integrations page, they cannot bind it to their own agents or use it in their own chats, and they never see the credential. Assignments, flows, the web widget and channels already act with the agent's integrations.
- **Switch to per-user.** When they chat, members use their own connected integration of the same provider; your account stays for the agent's assignments and flows. An imported OpenAPI integration cannot switch: each one is its own API.
- **Share the integration itself** (from the integration's page, see above) is a different grant: it becomes theirs to use everywhere, not only through this agent.

Through the API, an agent's owner lends with `lent_integration_ids` on `PUT /api/v1/agents/{id}`: only the owner, only their own integrations, only ones the agent binds (`[]` lends nothing). The agent reports it as `integration_lending` (the ids, who set it and when). Binding a new personal integration does not lend it until the owner chooses again; unbinding one stops lending it.

## Sharing a personal credential, everywhere

Any stored credential - an OAuth account, a personal access token, an API key, a bot token, a service-account secret - is used by more than the people an integration is shared with. Everything that carries the integration lends it to its own audience:

- **An agent** bound to it: its assignments, its flows, its public web widget, the people who message it on a channel without an account and the platform MCP key all act with the agent's integrations. In a chat, someone the integration is not shared with uses their own account instead, or is told under the answer that it was not used.
- **An assignment** that overrides the agent's integrations or listens to an integration's events, for everyone who can use the agent.
- **A flow** node, for the flow's audience.
- **A knowledge base** import source: its readers see what is synced with it.
- **A skill**'s private source: it is refreshed with it.
- **The General Assistant**'s platform bindings, for every user, and a user's own assistant, for that user.

Sharing is never refused. Before a change that lets new people act with someone's credential - sharing an integration, an agent, a knowledge base or a skill, making one company-managed, binding an integration to an agent, an assignment, a flow node, an import source or the General Assistant, enabling an agent's web widget, transferring an owner - one confirmation says whose credential it is ("your GitHub token", "Alice's Jira connected account"), who will be able to use it (with counts, and the public link, anonymous channel users or the MCP key when they apply), through what, what they can do with it (the provider, and the scopes when known) and how to avoid it: `Don't share my connected account`, switching the agent to per-user integrations, or a company integration. Nothing is asked when nobody new is reached.

The integration's `Overview` has a **Sharing and access** section for the people who manage it: every way the credential reaches people, with the reason (shared directly, bound to an agent, an assignment override or trigger, a per-user binding an assignment resolves to the person it runs as, a flow node, an import source, a skill source, the assistants), the audience and a link. An entity you cannot open is named by its owner only. The integration cards show the same thing compactly (`Shared - 3 ways`), with the breakdown on hover.

The same answers are available to API clients, read-only: `GET /api/v1/integrations/{id}/exposure`, `GET /api/v1/integrations/exposure/summary` and `POST /api/v1/integrations/exposure/preview` (see [Sharing and access](sharing-and-access.md) for every moment you are asked, and the routes every lent resource has).

## Scope(s): boundary or guidance

An agent, and each of its assignments, can narrow an integration to specific elements in its `Scope(s)` card (repositories, channels, CRM object types). The card says which kind of selection it is.

By default every selection is **Guidance only**: it is given to the agent as instructions ("focus your work on these specific resources"), and its tools can still reach other elements. This is also what every selection made before the choice existed stays.

A selection of one of the four element types below can instead be a hard boundary: turn on **Enforce as a boundary** next to it, in the agent's or the assignment's `Scope(s)` card, and save. The card then reads **Enforced**. The choice is made on each selection, so an agent can enforce its repositories while its Slack channels stay guidance. The Security Audit lists, for information, the selections of these types that are kept as guidance; it does not change the audit's score.

- **Enforced** - GitHub repositories. The agent's GitHub tools cannot work on any other repository, in assignments and in chat. A request for another repository is refused with the list of allowed ones, whichever way it names that repository. Repository listings and searches, a team's repositories, sub-issues and project boards show only the selected repositories, and a project card of another repository cannot be changed (draft cards belong to no repository and stay available). The tools that reach repositories without naming one - the raw GraphQL query and its query-only sibling, repository search, the notification inbox - are not offered, and creating or transferring a repository, changing a team's members and comparing with a branch of another repository are refused. Organization, team, project and user details stay readable.
- **Enforced** - Slack channels. The agent's Slack tools cannot read, post to, react in or manage any other conversation, in assignments and in chat; a channel may be named by its ID or its `#name`. Channel listings and message and file searches show only the selected channels, listing files needs a selected channel, and a file named by its ID is read or deleted only when it is shared in one of them. Direct messages and creating a channel are not offered. Workspace members stay readable. A Slack channel bot the agent posts through is bound by the bot's own publish channels, not by this selection.
- **Enforced** - Microsoft Teams channels. The agent's Teams tools cannot read, post to, reply in or list the members of any other channel, in assignments and in chat; a channel meeting's recordings and transcripts are read only in a selected channel (by name or ID), and so is an image pasted in a channel message. The teams-and-channels listing shows the selected channels. Teams chats (1:1 and group) and creating a channel are not offered. The selection applies to Teams only: Outlook mail, calendar, OneDrive, SharePoint, To Do and your own online meetings keep their tools.
- **Enforced** - HubSpot CRM object types. The HubSpot tools of an object type that is not selected (contacts, companies, deals, tickets) are not offered, in assignments and in chat, and associations, record deletion, record attachments and the associations a record read includes are refused for any other object type. Marketing email campaigns and the file manager (file search and upload) are not a CRM object type and are not offered; a file is downloaded through the record of a selected type it is attached to. Emails, notes, calls, meetings and tasks are read through a record of a selected type (its timeline, an engagement search on it, its associations), and one associated with no such record is refused. Forms and their submissions, sequences and the inbound report are offered only when contacts are selected. Owners, teams, pipeline and property definitions stay readable. A custom object is selected and named by its object type ID (`2-...`).
- **Guidance only** - every other element type (GitHub organizations, Jira projects, ...): these have no switch and cannot be enforced. The selection is given to the agent as instructions, but its tools can still reach other elements. Do not rely on it as an access boundary; restrict the integration's own credentials instead.

An assignment refines the agent's selection, and **the stricter of the two wins**: a type is a boundary for an assignment's runs when the agent or the assignment enforces it. A type the agent enforces is a boundary for every assignment of that agent (the assignment's switch shows it on and locked), and an assignment can enforce a type its agent keeps as guidance. The boundary is then the assignment's refinement of the agent's selection, and one that leaves no valid element (an entry that is not a valid `owner/name`, or an assignment enforcing repositories its agent does not offer) allows nothing.

The work an agent hands over - to a consulted or transferred agent, or as a background task - stays within the boundaries of the run that handed it over, narrowed further by the other agent's own selection of those types. A selection the delegating run only has as guidance does not follow the work: the other agent keeps working from its own selection, as the prompt it receives says.

!!! warning "What the boundary does not cover"
    The boundary applies to the GitHub integration's own tools. It does not restrict another route to GitHub the agent may have - an MCP server that exposes GitHub, a custom tool, the code interpreter or the web tools - nor a flow's integration nodes, nor the General Assistant, which has no `Scope(s)`. It does not govern the selected repository's own automation either: a workflow the agent starts runs with that repository's own token and secrets, and GitHub serves the commits of a repository's forks through the repository itself. After a transfer, the conversation continues under the new agent's own selection.

## Who receives an integration's events

An assignment with an integration-event trigger listens to the integration its trigger names, or, when it names none, to its agent's first integration of that provider. A flow's integration-event trigger names one too. XTM One reads those events with the integration's credentials and hands them to a run, and checks whether the run's user can see the integration:

- for an assignment, the user its runs act as: its `Run as` user, else its owner (the agent's owner);
- for a flow, the flow's owner.

Seeing the integration means the usual rule: it is theirs, it is shared with them or one of their groups, or it is company-managed and not restricted to an audience. Admins see every integration. A company-managed assignment with neither an owner nor a `Run as` user keeps the connected-account rule above, as before.

When that user cannot see it, the events are still delivered, as they always were, and never silently: the worker log names the assignment (or the flow and its trigger node) once an hour, and the security audit (`GET /api/v1/security-audit`, `Settings` > `Security`) lists it as a warning. Share the integration with that user, or pick one they can see.

## OpenAEV tenants

On a multi-tenant OpenAEV, every OpenAEV tool request acts on one tenant: the one in the integration's **Tenant ID** field (the tenant's ID in OpenAEV).

- **Left empty** (the default), the tenant is chosen the way OpenAEV does for a request that names none: the account's only tenant, or the default tenant when the account belongs to several, it among them. A single-tenant OpenAEV, and an account in the default tenant, need nothing more.
- **Set**, every request goes to that tenant. The account must belong to it: otherwise OpenAEV answers `TENANT_ACCESS_DENIED`, and the error names the tenant the request went to.
- An account that belongs to several tenants, none of them the default, needs the field: until then creates fail, with an error that says so.

Where to set it:

- **OpenAEV registered with XTM One** (the default `xtm_one` mode): the integration is created and kept up to date by the registration and its page is read-only, except the **Tenant ID** field, which an administrator edits there. A new registration keeps it. Its token is each user's own, so with the field set, every user of the integration works in that tenant and must belong to it.
- **API-key integration** (`copilot` and `dev` modes): the field is on the integration form. To work in several tenants, create one integration per tenant and bind each to the agents that work there.

## OpenCRQ tools

An OpenCRQ integration follows the OpenCRQ instance it is connected to. The `OpenCRQ version` card of its `Overview` tab shows the connected version, whether XTM One holds the description of the instance's operations, the tools hidden from your agents because the instance cannot serve them, and the fields a tool sends that the instance does not accept. A tool that sends such a field is refused before anything is sent. The card is shown to everyone who can open the integration, and turns amber when the description is pending, unavailable, or not kept because OpenCRQ registered with the default registration token. For an OpenCRQ that is not registered with XTM One, the description is read from the instance at the first OpenCRQ call of each conversation or run. How each instance describes itself is in [OpenCRQ tools follow each instance](../../deployment/xtm-suite-integration.md#opencrq-tools-follow-each-instance).

## XTM Hub

The XTM Hub integration gives agents the catalog of the [XTM Hub](https://hub.filigran.io), the Filigran ecosystem hub: the ready-to-use resources it publishes for the XTM Platform, namely OpenCTI custom dashboards, custom views, playbooks and integrations (connectors, CSV / TAXII / RSS feeds, live streams, third-party integrations), and OpenAEV scenarios. An agent bound to it can:

- list the catalogs;
- search one catalog, or all of them, by text, use case and compatible OpenCTI / OpenAEV version, and for OpenCTI integrations by type, verified status, connector-manager deployment and license;
- read one resource in full, from its slug or its XTM Hub link;
- list the newest and the most deployed resources, and the use cases resources are tagged with.

These need no account: keep the authentication mode `None`. Leave the XTM Hub URL empty to use the platform's default XTM Hub (`https://hub.filigran.io` unless [`XTM_HUB_DEFAULT_URL`](../../deployment/configuration.md#xtm-hub-registration) names another), or set the address of another XTM Hub.

With an XTM Hub API token, the agent also works as you in one of your XTM Hub organizations. It can:

- say whom the token acts as, in which organization, and whether it is read only;
- list the libraries you reach, private organization libraries included, and search their resources;
- list the platforms registered with the XTM Hub in that organization;
- download a resource's file as a file of the conversation or run, which other tools can then use. A downloaded OpenCTI dashboard can be imported into OpenCTI by its file: `import_opencti_dashboard` takes a file of the conversation as well as JSON, and checks it the same way;
- with a read and write token, publish a file of the conversation (an OpenCTI dashboard, view or playbook export, an OpenAEV scenario) to a library of yours where you may upload. Each publication is made in your name and stays inactive until someone activates it on the XTM Hub: an agent cannot publish a resource visible at once. Updating or deleting a published resource is done on the XTM Hub.

Once an administrator turns on [tool approvals](../../administration/ai-governance.md), the download and the publication each wait for your approval; the other XTM Hub tools only read and run without asking.

To set it up, open your profile on the XTM Hub, create a token under `API tokens` for the organization agents should act in, switch the integration's authentication mode to `API Key` and paste the token (it starts with `xhp_` and the XTM Hub shows it once). A read only token is enough for everything but publishing. The token never signs in, manages tokens or changes who can access what. XTM One sends it only over HTTPS, or to an XTM Hub on the same machine, and only once the XTM Hub says it accepts API tokens. A token belongs to the XTM Hub it was made on: changing the integration's XTM Hub URL, or switching back to `None`, removes it. A token on a shared integration acts as you for everyone it is shared with, so prefer a read only token there.

`Test connection` checks that the address answers as an XTM Hub and lists its catalogs, and with a token names the user and organization it acts as.

Each public result carries the resource's XTM Hub page. Deploying a resource into OpenCTI or OpenAEV is done from that page, with your own XTM Hub session.

Resources are published by the Filigran community. Their names and descriptions reach the agent as untrusted data, never as instructions.

The integration also appears on the [home dashboard](../home-dashboard.md), next to OpenCTI, OpenAEV and OpenCRQ.

The integration also exists as an import bundle, to distribute it as a package: build it with `python -m scripts.build_xtm_hub_bundle` from `backend/` (it writes `xtm-hub-integration-<version>.zip`) and import it as an administrator. The bundle creates the `custom:xtm-hub` provider and an `XTM Hub` integration pointing at the public XTM Hub. Its tools send the same catalog queries as the built-in integration, each a fixed query whose variables alone the agent chooses, but each search needs the catalog's id from `list_xtm_hub_catalogs` first and their answers are the XTM Hub's JSON, read with a size limit and handed to the agent as untrusted data, like the built-in tools' answers. Its tools need bundles that support constant values and untrusted answers, which arrived with this integration: on an earlier XTM One the bundle imports, but its tools fail.

## Good habits

- Read the status badge before editing anything.
- Use `Test connection` before assuming the agent is broken.
- Use the available tools card to understand what the integration really provides.
- Check scopes and re-authorization warnings when OAuth behavior changes. The platform also marks an OAuth integration as needing re-authorization, and stops refreshing its token, when the provider says the refresh token is revoked or invalid, or rejects the integration's client ID / secret (`invalid_client`). GitHub and Slack report both inside an otherwise successful answer, and the platform reads them there too: `bad_refresh_token` (GitHub) and `invalid_refresh_token` (Slack) for the refresh token, `incorrect_client_credentials` (GitHub), `invalid_client_id` and `bad_client_secret` (Slack) for the client ID / secret. In that last case, update the client ID / secret in `Configuration` first, then re-authorize: a new consent with the rejected credentials fails the same way. The mark stays until a new authorization succeeds: the platform never retries such an integration on its own, and saving a new client secret, token URL or scopes alone neither reconnects it nor retries the refresh. Saving a new client ID removes the tokens issued to the previous app, so the integration then shows as configured but not yet authorized until you authorize it. A temporary provider failure (a network error, an outage, rate limiting, such as Slack's `ratelimited` or `service_unavailable`) never produces this mark; the platform just retries later.
- Look at the `Agents` count on `Overview` before editing a shared integration.
