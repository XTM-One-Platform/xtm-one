# Agent accounts

A channel bot is an application with its own identity: people talk *to the bot*. An agent account is different: an agent operates a **real user account** of a messaging platform, or a **mailbox**, through that account's own sign-in. Two modes share one engine:

| Mode | The account is | Typical use | Starts with |
|------|----------------|-------------|-------------|
| **Delegate** (`on_behalf`) | Yours | "When someone mentions me or messages me on Teams, let my agent answer for me." "Let my agent answer the emails it can, and flag the ones I must answer myself." | Your approval before anything is posted, an AI label, a 2-minute grace period, the agent stepping back when you write |
| **AI user** (`ai_user`) | A dedicated service account everyone knows is an AI, with its own mailbox and calendar | An AI colleague that answers direct messages, follows whole channels and decides when to speak - or runs a shared mailbox (`support@`, `executive-assistant@`) | Replies posted at once, no label, direct messages and mentions answered |

Agent accounts are managed in `Profile` > `Agent Accounts`. Every user manages their own; administrators see every account with `All accounts`.

Two platforms are supported: **Microsoft Teams** (chats and channels) and **Microsoft Outlook** (a mailbox, see [Mailboxes (Microsoft Outlook)](#mailboxes-microsoft-outlook)). The engine is platform-agnostic: other platforms plug into the same rules, approvals and settings.

## Before you start

- **An integration signed in as the account.** Connect a Microsoft integration (`Integrations` > `New integration` > `Microsoft`) and sign it in as the account the agent operates: yourself for a delegate, the service account for an AI user. The integration's token must be one you may use - your own integration, or one shared with you with its owner's consent. The scopes the agent needs (`Chat.Read`, `ChatMessage.Send`, `ChannelMessage.Read.All`, `ChannelMessage.Send`, `User.ReadBasic.All` for Teams; `Mail.Read`, `Mail.ReadWrite`, `Mail.Send` for Outlook) are already part of the Microsoft integration.
- **An agent.** Its persona, knowledge bases and tools answer. Messages come from other people, who can write anything: prefer an agent whose tools you are comfortable running for them (read-only integration bindings, no destructive tools), and keep approval on for a delegate until you trust it.
- **A public HTTPS address for real time.** Microsoft Graph only notifies an HTTPS address, built from `BASE_URL`. Without one, direct and group chats - and a mailbox's inbox - are polled every minute, and channels cannot be heard.
- A work or school account: Microsoft Graph does not offer these APIs for personal Microsoft accounts.
- Microsoft Graph allows one subscription per Microsoft application and chat or channel. When two agent accounts signed in through one application follow the same channel, only one of them hears it, and the other one's card says which account holds it (see [When the account does not hear a conversation](#when-the-account-does-not-hear-a-conversation)).
- **One agent account per platform account.** A platform account - and an integration, on each platform - is operated by one enabled agent account at a time. One Microsoft integration can therefore run a Teams account and an Outlook mailbox together, but not two Teams accounts. Saving a second one is refused, naming the first when it is yours (another user's account is never named): put the extra behaviour in rules of the first one, or save the second one switched off. Saving reads who the integration is signed in as; when Microsoft cannot say in time, the account is checked again when it starts listening, and stays in error if the platform account is taken.

## Operating a service account

You can operate several platform accounts from your own XTM One account: yours, and any service account you hold the credentials of. For example, Samuel signs in to XTM One as himself and runs the `executive-assistant@` mailbox:

1. Connect a second Microsoft integration, signed in as `executive-assistant@` (name it, for instance, `[Executive Assistant] Microsoft`).
2. Create an **AI user** account with that integration and the Executive Assistant agent. Under `Account (integration)` the editor shows who the integration is signed in as - check it reads the service account, not you.
3. Bind Microsoft on the agent (a per-user binding is enough) to give it the account's mailbox and calendar.

In that account's replies, the agent works as the service account:

- **Its Microsoft tools act as the account.** Mail, calendar and Teams tools run with the account's integration, whatever the agent's own Microsoft binding would resolve to for you - never your mailbox. An agent with no Microsoft binding gets no Microsoft tools: the account changes which account a tool acts as, never which tools the agent has.
- **It talks to the person who wrote.** The agent is told who wrote as the platform reports it, marked unverified, not that it talks to you. Your personal memory (what the assistant remembers about you) and your default knowledge bases and MCP servers are left out of these replies.
- **Your other agent accounts stay out of it.** Two agent accounts never answer each other: a message posted by any agent account is left alone by the others, so your delegate and the Executive Assistant cannot reply to each other in a loop.

Every reply still counts against your agentic quota, and its tools run with your permissions.

## Create an account

1. Open `Profile` > `Agent Accounts` and click `New account`.
2. Choose **Delegate** or **AI user**. The choice only sets starting values; every setting stays editable.
3. Under `General`, pick the platform (`Microsoft Teams`, `Microsoft Outlook` or `WhatsApp`; a new account's rules follow it), name the account, pick the integration signed in as the account and the agent that answers. The editor shows who the integration is signed in as, and warns when another agent account already operates that account.
4. Review the **Rules** (below), the **Replies** settings and, if needed, the **Schedule**.
5. Use **Test** to check what the agent would do with a message, then `Create`.

Saving answers at once. In the background, the account reads who it is from the integration, subscribes to the conversations its rules reach, and starts listening: its card shows `Setting up` meanwhile, then its status and how it listens (`Real time`, `Polling every minute`). Microsoft confirms each subscription by calling the platform back, which can take a few seconds per conversation. When a conversation cannot be heard, the card says why and what to do (see [When the account does not hear a conversation](#when-the-account-does-not-hear-a-conversation)).

## Rules

Rules are read top to bottom; **the first rule that matches a message decides**. A message no rule matches is left alone. Each rule matches on:

| Setting | Options |
|---------|---------|
| **Where** | Direct messages, group chats, channels - any combination |
| **Specific conversations** | Chosen from the account's own chats and channels. Empty: every conversation of the selected kinds |
| **Only these people** | Focus the rule on some people (chosen from the directory, or typed as an email) |
| **Members of these groups** | XTM One groups: the rule also covers a sender one of whose Microsoft addresses (mail address or sign-in name) is the email of an active member. With no people and no group, the rule covers anyone |
| **Except these people** | People this rule never answers |
| **Trigger** | `Only when the account is mentioned`, or `Every message (full monitoring)`. A direct message always counts as addressed to the account. In channels, a rule waiting for a mention hears the mentions only: the channel's other messages - alerts and bot posts included - never reach the account, unless another rule answers every message there |
| **Action** | `Answer`, or `Ignore (stop here)` - an ignore rule placed first is how you exclude a conversation or a person entirely |
| **Reply** | `Always reply`, or `AI decides whether to reply`: the agent reads the message and stays silent unless its persona says it should speak |
| **Follow the thread** | Once the agent answered in a thread, later messages there count as addressed, and the agent decides whether to answer them. In a channel heard for mentions only, the platform checks that thread every minute for a day after the agent's last answer there, so a follow-up without a mention can take up to a minute to be seen |

A rule can also override the account's defaults for the messages it answers: the response mode, the AI label, the grace period, and extra **guardrails and instructions** ("for my manager, be formal and never commit to a date"). The sparkle button next to the account's instructions and to each rule's guardrails writes or improves them with AI from a short description.

The editor offers ready-made rules: **Monitor a channel (AI decides)**, **Focus on some people**, **Ignore a conversation**, **Public channels, non-confidential only** and **Leadership groups, full access** (both described below).

### Access by audience

An agent with deep reach - board material, every integration, a long memory - should not answer everyone with everything. Besides who it answers, each rule decides **what the agent may use** for the messages it covers, in its **Access** section:

- **Integrations, knowledge bases, MCP servers, built-in tools, custom tools and skills.** The section lists the agent's own; everything is on by default, and switching one off removes it from those replies. This is enforced by the platform, not asked of the model: a knowledge base switched off is not searched, an integration switched off has no tool. A rule only ever narrows the agent - a capability the agent gains later is on for a rule that left the list untouched, never for one that narrowed it.
- **Memory.** `Use the agent's memory` off: the agent neither reads nor writes its memory for those messages, so what it learned in a board chat cannot surface in a public channel, and nothing said there is remembered.
- **Guardrails.** The rule's instructions govern what the agent says to that audience ("only information already public inside the company; decline board, financial and HR questions politely"); generate them with AI from a one-line description.

Because the first matching rule decides, **order is the policy**: put channel rules first. An executive asking the Executive Assistant a question in #general then gets #general's access, not theirs, because everyone in the channel reads the answer. For example:

1. **Public channels, non-confidential only** - channels, when mentioned: no confidential knowledge base or integration, memory off, a guardrail.
2. **Leadership groups, full access** - direct and group chats, for the members of the `Board` and `ExCo` groups: everything the agent has.
3. Nothing else: anyone outside those groups messaging the account directly is left alone.

The **Test** tab shows which rule applies and whether it limits access; type an email as the sender to test with the groups of the XTM One user it belongs to. When a rule names groups, it also says who the sender was found to be: `XTM One user ..., member of Board`, `in none of the groups the rules name` (add them to the group), or `No active XTM One user has the email ...` (their XTM One account uses another email, or is deactivated).

### Examples

- *Delegate, mentions and DMs only:* the default rules. Add **Focus on some people** above them with `Send automatically` for your team, so only their questions skip your approval.
- *AI user following an incident channel:* a rule on that channel with `Every message` and `AI decides`. The agent reads everything and speaks when it has something useful to add.
- *Never in #general:* an `Ignore` rule on that channel, first in the list.

## WhatsApp business numbers

A WhatsApp agent account operates the business phone number of a `WhatsApp` integration (WhatsApp Cloud API): as an **AI user**, the number is the agent's own (a support or assistant number); **on behalf**, it is a person's business number and the agent answers as them.

- **What it hears.** Every message the number receives, in direct conversations (one per phone number) and in the groups the number created, as soon as Meta delivers it to the integration's webhook. No subscription to set up: connect the integration's webhook once (see the WhatsApp integration page) and the account hears what the integration stores. Reactions are not answered.
- **What it sends.** A text reply, shown in WhatsApp as a reply to the message answered. What agents send through the integration's tools (messages, templates, files, buttons) appears in the account's activity log.
- **Context.** The agent reads the conversation's previous messages as the integration stored them; WhatsApp does not return what the number itself sent.
- **Linked-device gateway backend.** An integration whose backend is `linked_device_gateway` needs no phone number id: the account reads the paired number from the gateway, replies through it and hears what the gateway webhook stores. Every sync checks the number is still paired; an unpaired number puts the account in error and asks to pair it again. The account shows `Real time` once its rules answer in conversations, on both backends. A refused check names the backend it checked (`Cloud API` or `linked-device gateway`).
- **Limits.** WhatsApp allows a free-form reply only within 24 hours of the person's last message; after that, only an approved template can be sent.

## Mailboxes (Microsoft Outlook)

An Outlook agent account watches the account's **inbox** and handles each email its rules give the agent - your own mailbox as a delegate, or a service account's (`support@`, `executive-assistant@`) as an AI user.

**Where.** A mailbox has two kinds of conversation instead of chats and channels: `Emails to the account alone` (the account is the only recipient) and `Emails with other recipients`. The trigger reads `Only when the account is in To` or `Every email, Cc included`: an email to the account alone always counts as addressed to it, an email where it is only in `Cc` does not. A thread of replies is one conversation, named after its subject.

**Starting rules.** A new mailbox answers the emails sent to the account alone and those with the account in `To`, the agent deciding for each; emails where the account is only in `Cc` are left alone. As a delegate, replies wait for your approval, with the AI label, a 2-minute grace period and up to 10 replies per conversation and hour; as an AI user, replies are sent at once, up to 30. Either way, the agent takes up at most 60 emails an hour across the whole mailbox - each new email starts a conversation of its own - and a reply that would go to more than 10 people besides the sender (a reply-all) is handed to you instead of being sent.

**Who.** `Only these people` and `Except these people` take an address or a whole domain written `@example.com` (that exact domain, not its subdomains): for instance `Ignore` emails from `@newsletter.example.com`, or answer `@customer.com` only.

**Triage.** For each email a rule hands it, the agent makes one of three choices, and says why in one line:

| The agent | What happens | Shown in the account's page as |
|-----------|--------------|--------------------------------|
| **Answers** | The reply goes out as the response mode says (sent, waiting for your approval, or recorded in shadow) | `Answered` |
| **Hands it to you** - it cannot or should not answer (a decision only you can take, missing information, a sensitive topic) | Nothing is sent; by default the email is flagged for follow-up, filed in the category `Needs your attention`, and the notification bell tells you, with the agent's reason | `Handed to you` |
| **Leaves it** - it needs no reply (a thank-you, an FYI, a confirmation) | Nothing is sent; by default the email is filed in `No reply needed` | `The agent stayed silent` |

The choice is read by the platform from a structured answer, not from the wording of a reply. A rule with `Always reply` lets the agent answer or hand over, never leave an email unanswered. The agent's reason shows under `The agent's reason` when you open the email in the `Decisions` tab.

**What the mailbox shows** (`Replies` > `Mailbox triage`): for each of the three outcomes, whether the email is marked as read, the Outlook category it is filed in (empty for none; the category is added to the email's own), and for an email handed to you whether it is flagged and whether you are notified. By default an answered email is marked as read and filed in `Answered by AI`. In `Shadow` mode nobody is notified and the mailbox is left untouched, except that an email handed to you is still flagged for follow-up when flagging is on, so you find it. A flag the mailbox refuses shows the email as `Failed` in the `Decisions` tab, with the mailbox's error as the account's last error.

**Replies.** The agent replies in the email's thread - to the sender, or to everyone when others were on the email - with the signature below under the answer and the original email quoted below it, as Outlook does. The agent reads the thread's earlier emails, and the names of the email's attachments, as context. If you answer the email yourself during the grace period, or write in the thread, the agent steps back as on Teams.

**Signature** (`Replies` > `Signature`). A reply the agent sends never goes through Outlook, so a signature add-in installed in Outlook never signs it. The account chooses what it carries:

| Signature | What the reply carries | When to choose it |
|-----------|------------------------|-------------------|
| **The account's own** (default) | The signature of the account's latest sent emails, copied | Signatures set in Outlook, or a service account whose sent mail is signed |
| **From a signature integration** | The person's current signature from a [Letsignit integration](../../usage/resources/integrations.md#email-signatures-letsignit), read when each reply is sent: their signature and banner as Letsignit composes them now, its reply variant first | Letsignit signs your mail in Outlook (the add-in): the reply looks like one the person sent, banner of the day included |
| **None** | No signature | The mail server signs every outgoing email in transit (Letsignit's server-side signature, an SMTP connector): the reply is signed there, once |

The signature is looked up by the account's email address and kept for 10 minutes. When the integration cannot be read, or holds no signature for that address, the reply carries the account's own, and the account's card says why until a reply carries the integration's signature again.

**Left alone.** The account's own emails, drafts, meeting invitations, meeting responses and cancellations with nothing written in them (a bare accept or decline), emails sent by any agent account (each carries a header saying so, so two agent mailboxes never answer each other in a loop), and **automated mail** - newsletters, mailing lists, notifications, auto-replies and out-of-office messages, recognised by the headers such mail carries (RFC 3834) - shown as `Automated mail (a notification, a newsletter, an auto-reply)`. A meeting response or cancellation in which the sender wrote something ("Declined: ..." with new times proposed) is heard like any email, from the person who answered: the delegate when it was sent on behalf of someone, the rules matching either.

## Replies

| Setting | What it does |
|---------|--------------|
| **Response mode** | `Send automatically`; `Ask me first` - replies wait in your approval queue; `Shadow (record only)` - nothing is posted, so you can see what the agent would have said while you tune the rules |
| **AI label** | A line such as *Sent by an AI agent on behalf of Sam* added to every reply, before or after it. It is added by the platform, never left to the model. `{name}` stands for the account's name. A rule can turn it on or off |
| **Grace period** | Wait before answering. If the account's human answers meanwhile, the agent stays silent. A restart during the wait does not lose the message |
| **Step back after the owner writes** | Once the human writes in a conversation, the agent stays out of it for this many minutes |
| **Replies per conversation and hour** | A safety limit, for instance between two AI accounts. `0`: no limit |
| **Agent turns per hour, whole account** | Every message the agent takes up costs a turn - and an AI quota - whether it answers, hands the message to you or leaves it. Past the limit, the account leaves the next messages until the hour ends, shown as `Hourly limit of the account reached`. `60` for a new mailbox, where anyone can write new emails; `0` (no limit) for a new Teams account |
| **People a reply may reach** | Mailboxes. The sender of an email chooses who a reply-all reaches: a reply that would go to more people besides the sender is handed to you - flagged, with the reason - instead of being sent. `10` by default; `0`: no limit |
| **Context messages** | How many earlier messages of the thread or chat the agent reads. The conversation is replayed from the platform - messages the human wrote themselves included - so the agent answers in context, as a participant. The agent's own earlier replies are part of it (on WhatsApp, read from the account's record of what it sent), so only the messages that arrived since its last reply are the ones it answers |
| **Instructions** | Added to the agent's persona for every reply of this account; can be generated with AI |
| **Reactions as feedback** | On by default. A reaction someone puts on an answer of the agent rates it: by default a thumbs up is positive and a thumbs down negative; click an emoji to make it positive, negative or ignored. See below |
| **Notify me when a reply is sent** | On by default. Each reply the account posts on its own (`Send automatically`) reaches you in the notification bell - where it was posted, to whom, and the start of what was said - so you can follow what is said on your behalf without reading every conversation. A reply you approve does not notify you again |

**Files and images.** An image pasted into a Teams message and a file shared in it reach the agent with the message: each is downloaded as the account (the files with `Files.Read.All`, already part of the Microsoft integration, the pasted images with the chat's own `Chat.Read` or `ChannelMessage.Read.All`), kept as a file of the reply's turn under your name, and handed to the agent, which reads it with `read_file_content` and sees an image when its model takes images. At most 10 per message, each under the upload size limit; one that cannot be downloaded is left out and the agent answers with the rest.

**Voice notes.** A voice message recorded in a Teams chat, or an audio file sent to the account (m4a, mp3, mp4 audio, ogg, opus, wav, webm), is downloaded as the account and transcribed for you with the platform's speech-to-text (the OpenAI provider you may use, as for dictation in the chat; the language is detected). The agent reads it as the message's text, marked `[voice note, 0:42, transcribed] ...`, after any text the message also carries; it is queued and merged with your other messages like a text. The audio is not kept as a file. When the audio cannot be downloaded or transcribed (no OpenAI provider, a refused download), the agent reads `[voice note: transcription failed: <reason>]` and can tell you so.

### Reactions as feedback

When people react to an answer the agent posted, their reaction becomes a rating of that answer in the agent's **Feedback** tab (and in what the agent's persona improvement reads), next to the thumbs given in the web chat.

- Anyone who can react counts, colleagues without an XTM One account and guests included. The platform keeps only a keyed fingerprint of who reacted - enough to count one rating per person and to remove it when the reaction is removed - never their name or id.
- A skin tone counts as the same emoji. Someone holding a positive and a negative emoji on the same answer does not rate it. Emojis you did not map are ignored.
- A reaction counts for 14 days after the answer was posted. Switching the setting (or the account) off stops reading reactions and keeps the ratings already given. Switching it back on, or changing which emojis count, reads the reactions of every answer still within its 14 days again, so the ratings follow the setting; older answers keep the ratings they had.
- In `AI user` mode, the account's own reactions are not ratings (the account is the agent). In `On my behalf` mode, your own reaction on your delegate's answer counts.
- Reactions are read as they happen in chats and in channels the account hears in full. In channels heard for mentions only - Microsoft allows one subscription per channel, and that one only carries mentions - and when the platform has no public HTTPS address, the answers are checked every few minutes, then less often as they age. Answers heard as they happen are also checked every few hours, in case Microsoft stops notifying their changes. If Microsoft refuses to notify the reactions of a chat or channel, the account still hears it, its answers are checked instead, and the account's listening problems say so.
- The account page shows how many positive and negative ratings each answer received (`Messages sent` tab), and the totals over the period (`Overview` tab).

The agent never answers the account's own messages, nor messages from apps, bots, workflows and other agent accounts.

## Approvals

With `Ask me first`, each reply waits in `Replies waiting for your approval` at the top of `Profile` > `Agent Accounts`, and the notification bell tells you. For each one you see the message, the conversation and the rule, and you can **send** the draft, **edit** it first, or **drop** it. The AI label is added when the reply is sent. A reply nobody approves expires after a day. `Shadow` drafts can also be sent from the `Replies` tab of the account's page.

## Active hours

A weekly schedule with a timezone. `During these hours` answers only inside the windows; `Outside these hours (when I am away)` answers only outside them - for instance evenings and weekends for a delegate. A window that ends before it starts runs past midnight.

## Understanding what happened

The name of an account, or its `Activity` button, opens the account's page. It keeps every message the account heard over the last 30 days, in three tabs:

- **Overview** - over the last 24 hours, 7 days or 30 days: how many messages the account heard and how many were for the agent, what it did with them (answered, waiting for your approval, stayed silent, handled elsewhere, left alone, failed) and the share it answered, the median time to answer and to approve, the ratings of its answers. Charts show the activity over time, the kinds of conversation and why messages were left alone; tables show what each rule decided and the conversations and people the account heard the most. A figure, a conversation or a person opens the `Decisions` tab on it. Hide the messages left alone from the timeline to see only what the agent did.
- **Decisions** - every message, newest first, filtered by what the account did, the kind of conversation, a conversation, a person or a search, page by page. `Handled elsewhere` is an email another pipeline of the same agent took first (an assignment run on the mailbox, or another of its agent accounts): this account sent nothing, and the opened row says who took it (`Taken by an assignment run of this agent.`, or `Taken by the agent account <name>.` with a link to that account; an account you cannot see reads `Taken by another agent account of this agent.`). The outcome filter wraps onto a second row when its options do not fit. A row opens on why it happened: the reason (`The account is not mentioned`, `No rule covers it`, `The account owner took over`, `Hourly reply limit reached`...), the rule, how the message was addressed (a direct message, a mention, a follow-up in a thread), whether a grace period ran, and the reply it produced. When a rule names groups, it also shows who the sender was found to be, as in the **Test** tab.
- **Messages sent** - every message the AI sent in the account's name, filtered (`All`, `Replies to a message`, `Proactive sends`, `Failed`, `Awaiting approval`, `Shadow`) or searched: the replies to a message the account heard, with what was posted and how it was rated, and the messages the agent sent on its own (a follow-up or an opening message from a chat, an assignment or a tool call), shown as `Proactive send` with their recipients and origin in place of the quoted message; the drafts waiting for you (send, edit or drop them here), shadow drafts, and the sends that failed with why. A proactive send is also a row of **Decisions**, where it opens on what was sent rather than on why, the rule and how it was addressed. A failed send is logged as `Failed`; a log that cannot be written never fails the send and leaves a warning in the server logs. Messages sent before this log existed are not backfilled.

Use **Test** in the editor to try a message against unsaved rules.

A message from a conversation no rule covers reads `No rule covers it`, whoever wrote it. Seeing some after narrowing the rules (for instance from a channel you no longer follow) means the account was still subscribed to it: the first such message triggers a new synchronization, which drops the subscription within a minute or two.

## Manage accounts from a chat

Agents can read and change agent accounts for you, so you can ask in a conversation instead of opening the editor: "pause my Teams delegate until Monday", "stop answering in #general", "only answer my team in direct messages", "why didn't you answer Bob yesterday?", "create an Outlook account for the Executive Assistant".

- **The General Assistant, and any agent with the agent builder** (`Agent builder` built-in tool), manage every account of yours: list them, read an account (its settings, its rules in order, whether it hears its conversations), create one, change any setting, enable or disable it, add, change, move or remove one rule, delete it, test a sample message against the saved rules or a proposed change, and read the activity log. The same tools are on the XTM One MCP server; a read-only API key gets the reading half.
- **An agent that operates one of your accounts** gets the same tools, except creating an account, for the accounts it operates for you only - with no extra built-in tool to enable. Ask the Executive Assistant, in a chat with it, to "answer emails from @customer.com without waiting for my approval" and it changes its own rule. To keep an agent from changing its accounts, add the tools to its tool deny list (`update_agent_account`, `edit_agent_account_rule`, `delete_agent_account`).
- **Never for the people writing to the account.** While the agent answers a message someone sent to the account, none of these tools is offered, not even the reading ones: the person writing could otherwise read your rules or widen them. That reply only knows the rule it answers under, and whether the platform adds the AI label to it.

The agent's changes are the editor's: the same checks (an integration and an agent you can use, one agent account per platform account, valid rules), saved the same way, and recorded in the audit log as made through an agent. The `Agent Accounts` page shows them as soon as the tool returns. A rule the platform cannot apply changes nothing, and the agent is told why.

## When the account does not hear a conversation

Each synchronization checks every conversation the rules reach. When some cannot be heard, the account card says how many (`Not listening to 12 of 14 conversations`), when it last checked, and one entry per cause: what is wrong, the conversations it affects (`All chats`, or the channel by name), what to do, and the button that does it. `Technical details` shows what Microsoft answered, word for word, for support. While a new check runs, the card keeps the previous check's findings and says so. Once fixed, use `Synchronize now` (or wait for the next synchronization, within the hour).

| The card says | What it means | What to do |
|---------------|---------------|------------|
| **The account's integration cannot be used** | The integration is gone, disconnected, or its token is not one you may use | Open the integration and sign it in as the account, or pick another one in the account's settings |
| **Microsoft Teams rejected the integration's sign-in** | The sign-in expired or was revoked | Sign the integration in again as the account |
| **Microsoft Teams refused to notify the account** | Microsoft refused the subscription: the integration lacks the permission the card names (`Chat.Read` for chats, `ChannelMessage.Read.All` for channels - often an administrator's consent in Entra ID), or the account is no longer a member of the conversation | Get the permission granted, sign the integration in again, then synchronize |
| **Your agent account ... already follows it** / **Another user's agent account already follows it** | Microsoft allows one subscription per application and conversation, and another agent account of this platform holds it | Remove the conversation from one of the two accounts' rules, or connect one of them through another Microsoft application |
| **... already follows it through the same app** | Another platform signed in as this account through the same Microsoft application (for instance a staging instance) holds the subscription | Remove it there, or use another Microsoft application for one of the two |
| **Microsoft Teams could not reach the platform to confirm the subscription** | Microsoft calls the platform back before it creates a subscription, and the call never arrived: `BASE_URL/api/v1/agent-accounts/notifications/microsoft_teams` is not reachable from the internet over HTTPS (behind a VPN or an access proxy, for instance) | An administrator makes `BASE_URL` publicly reachable over HTTPS |
| **Channels cannot be heard without a public HTTPS address** | `BASE_URL` is not a public HTTPS address: chats are polled every minute, channels cannot be heard | An administrator sets `BASE_URL` to a public HTTPS address |
| **The conversation no longer exists, or the account left it** | The channel or chat is gone, or the account was removed from it | Remove it from the rules, or add the account back |
| **The rules cover it, but the account does not listen to it** | A rule answering in every channel follows the first 50 channels of the account; a channel past them, every channel of a team whose channels Microsoft refuses to list, and a channel a rule names without its team are not subscribed to. `Technical details` says which | Name the channel in a rule, or pick it again in the rule |
| **Microsoft Teams asked the platform to slow down** / **did not answer** | A temporary condition | Nothing: the next synchronization tries again |

The account keeps hearing every conversation that is not listed: a card with problems and the `Active` status still answers there.

## Security notes

- An agent account acts with its owner's integration token and agent. It is never shared; administrators can see and edit every account.
- Messages from other people are untrusted input to an agent that acts as you. The platform's prompt-injection checks and content quarantine apply as on channels, and a tool that needs approval stops the reply instead of posting an explanation - but the safest setup is a delegate with approval and an agent with read-only tools.
- An account's configuration is yours: the agent-account tools are never offered while the agent answers someone writing to the account (see [Manage accounts from a chat](#manage-accounts-from-a-chat)). Other tools of the agent still run with your permissions in those replies: narrow them per rule in **Access**.
- Group membership is read on every message: someone removed from a group stops reaching what that group could at their next message. A sender is matched to an XTM One user by the addresses the platform reports from its directory (for Microsoft, the mail address and the sign-in name), which the sender cannot type; a sender with no XTM One account belongs to no group. The decisions show only the groups the rules name, never a sender's other memberships.
- Access limits are enforced by the platform, guardrails by the model. Put what must not leak behind an access limit (a knowledge base switched off, memory off), and use guardrails for tone and for what the agent should decline to discuss.
- With AI switched off on the platform, no message is answered (each is recorded as `AI unavailable`); the accounts stay yours to create, edit, synchronize or delete, and their waiting replies to send or drop. Generating instructions with AI waits for AI to be back.
- Every created, changed, deleted, approved and rejected account or reply is recorded in the audit log, and every agent turn counts against the owner's agentic quota.
