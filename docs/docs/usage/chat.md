# Chat

Chat is where you work directly with XTM One in natural language. It supports both the platform-wide `General Assistant` and individual agents.

## What Chat is for

Use Chat when you want to:

- ask a question in plain language
- work with a specific assistant
- upload supporting files
- reuse a saved prompt
- continue an earlier conversation
- speak instead of typing, if your browser supports it

## The chat layout

Chat has two main areas:

- the left sidebar for assistant selection and conversation history
- the main panel for the active conversation

If you are on a smaller screen, the sidebar can collapse so the conversation has more space.

While an agent works, the conversation keeps its end in view: each new command card, output line, status change or wait notice scrolls in as it appears, in the browser and in the desktop app. Scroll up to read and the view stays where you put it, with a `Scroll to bottom` button; scroll back to the end or press the button and it follows again.

Each command or tool card an agent runs on a machine shows on one line: what it did (`Ran a command`, `Read a file`), the first line of the command or the path, the machine, the result (a spinner while it runs, then the exit code or the error) and how long it took. Click the line to see the whole command, its output and the copy and larger-view buttons; a card you open stays open while you are on the page.

## Choose who you want to talk to

At the top of the sidebar, you choose the assistant for the conversation.

You may see:

- `General Assistant`
- chat-enabled agents
- assignment-oriented agents shown in a `Not recommended for chat` section

An agent your company manages carries a `Company` badge. When an administrator links an agent to the General Assistant, that agent takes the General Assistant's place in the list and carries an `Assistant` badge.

Choose the `General Assistant` when you want a broad helper. Choose a specific agent when you want an assistant with a focused role or built-in context.

Picking another assistant in the sidebar while a conversation is open starts a new conversation with it. To keep working in the same conversation with another assistant instead, see [Continue a conversation with another agent](#continue-a-conversation-with-another-agent).

## Continue a conversation with another agent

A conversation keeps its whole history whatever agent answers in it. To hand the conversation you are in to another agent - because its agent was deleted or disabled, or because another agent should apply what you asked earlier - open `Conversation options` at the top of the conversation, choose `Continue with another agent`, pick the agent and click `Continue`.

- The agent you pick answers from your next message. It receives the earlier messages of the conversation, and reads the earlier answers labelled with the agent that wrote them, so it does not take them for its own.
- A note `Continued by <agent> on <date>` marks the change in the conversation.
- Only your own conversations in the web chat can be continued this way, between two answers. A Slack or Teams conversation is answered by the agent its channel routes to.
- The agents offered are the ones you can start a conversation with: enabled and open to chat.

When an agent is deleted, its conversations are kept. Its owner can name another agent to continue them (see [Agents](agents.md#delete-an-agent)); otherwise the conversation shows that its agent was deleted, and you continue it with an agent of yours the same way. The [Studios](presentations.md) offer the same action next to the conversation's agent (the arrows button).

## Ask an assistant about your earlier conversations

Every assistant you chat with can read your own conversations back, whichever agent you had them with, a deleted one included. Ask it, for example, "apply what I asked about the launch video in my conversation with Video Bot":

- `list_conversations` finds the conversation: by a word of its title, by the agent it was held with (`agent`, an id or a part of its name, a deleted agent by its last name) or by the video, presentation, report or file it is about (`object_id`, `object_type`). Each conversation is listed with its agent.
- `read_conversation_history` reads its messages, oldest first and a window at a time, the conversation you are in when no conversation is named. Each message comes with who wrote it (you, or the agent by name), its date, the tools an answer used and the tool results in one line each, and the files, videos, decks and reports it carried, so the assistant can open them with its other tools. `search` keeps only the messages containing a text. The full content of a tool result is never returned.

Only your own conversations can be read, never another person's, even one shared with you; apart from the conversation you are in, a Slack or Teams channel thread other people read is not read either. Reading a conversation other than the one you are in is recorded in the audit log (`read_conversation`), once per 15 minutes for the same conversation.

## Start a new conversation

To start a new conversation:

1. Open `Chat` from the sidebar.
2. Choose the assistant you want.
3. Click the `New conversation` button.
4. Type your message and send it.

If you open Chat from an agent card or an agent detail page, XTM One can preselect that agent for you.

## Manage conversations from the sidebar

The sidebar is not only a list of past chats. It is also your main control area.

From here you can:

- search conversations by title, preview text, or agent name
- open an earlier conversation
- organize conversations in workspaces (see [below](#organize-conversations-in-workspaces))
- archive a single conversation
- archive all conversations from the `Archive all conversations` action
- switch assistants without leaving the page

Archiving removes a conversation from the active list. It is not the same as simply closing the page.

The conversations other people shared with you are listed under `Shared with me`, below your own; the search narrows them by title, owner and last message too.

## Organize conversations in workspaces

To keep the conversations of one topic together, and let their agents share work items and shared state, file them in a [workspace](workspaces.md). The sidebar lists your conversations by workspace, a little like folders:

- `Workspaces` lists each workspace you can file into, your default first, with the conversations filed in it. Click a workspace's name to fold or unfold it; the sidebar remembers what you folded.
- `Not in a workspace` lists the conversations filed nowhere. That is a normal place for a conversation to be: a new conversation starts there, and it stays there until you file it.

From the sidebar you can:

- create a workspace with the `New workspace` button next to `Workspaces`: type its name and press Enter
- file a conversation by dragging it onto a workspace, or with the `Move to workspace` menu of its row, which also offers `Remove from workspace`; dropping a conversation on `Not in a workspace` takes it out of its workspace too
- start a conversation directly in a workspace with the `+` button of that workspace (`New conversation in this workspace`)
- manage a workspace from its `Workspace actions` menu: `Rename`, and the same actions as on the [Workspaces page](workspaces.md#manage-a-workspace) — `Make default`, `Knowledge bases`, `Share`, `Archive` and `Delete` — plus `Open in Workspaces`. Archiving asks for a confirmation, then takes the workspace out of the sidebar; its conversations are then listed under `Other workspaces`. To restore it, open `Work > Operations > Workspaces`, click `Show archived`, then choose `Restore` in its actions menu. Deleting asks for a confirmation and keeps its conversations, which then appear under `Not in a workspace`; a workspace that still holds work items cannot be deleted

Filing a conversation into a workspace other people can see asks first: its members can then search what was written in it.

Only your own conversations can be filed, into workspaces you can change. A workspace shared with you in read-only shows only while it holds one of your conversations, and a conversation filed in a workspace you can no longer see is listed under `Other workspaces`. While you search, only the workspaces holding a matching conversation are shown.

You can also file the conversation you are in from the `Conversation options` menu at the top of the conversation (`File in a workspace`). An assistant with the `XTM One` built-in tool can do it for you — switch the tool on in the [session tools menu](#use-session-tools), then ask for example "create a workspace called Acme audit and move this conversation into it".

## Write your message

The composer at the bottom of the screen supports several input methods.

You can:

- type a normal message
- attach files with the paperclip button
- paste files into the composer
- drag and drop files onto the page
- insert a saved prompt template
- use voice dictation
- start a full voice conversation

A message holds up to 30 files, each up to 40 MB: a batch of screenshots goes in one message. Past 30, the composer names each file it did not attach and says why ("a message holds at most 30 files"); send them in the next message. While `Deep Investigation` is on, the composer takes a subject, not files: remove the attachments, or turn it off to send them.

While a response is being generated, `Stop generating` (or Esc) interrupts it, and you can keep writing: see [Add to a reply that is still running](#add-to-a-reply-that-is-still-running).

If a response ends before its answer because XTM One restarted or the reply failed, the work the agent already did is kept: the next message you send in the conversation (`Go on`, for instance) gets the tools it called and what they returned, so the agent continues from there instead of starting over. A response you stop yourself, and one that ended more than 24 hours ago, start from scratch. A long piece of reasoning with no tool call cannot be kept.

A line under the composer reminds you that AI can make mistakes and that important information should be checked. The same notice appears at the bottom of the [embeddable chat widget](web-widget.md), between the input box and the "Powered by" line. It is shown in your interface language in the app and in English in the widget.

The lightning icon among the composer buttons counts the agentic actions consumed in the current quota period (the day, month or year your administrator configured), out of your limit when there is one. Hover it for the details. While an assistant works on your message, the action this reply consumes is shown in brackets: `4(+1)/10` means 4 actions before this reply, 1 for it, out of 10. It joins the total when the reply ends, along with any sandbox code runs counted toward the same limit; a reply that fails may have its action credited back instead (see [Configuration](../deployment/configuration.md#agentic-quota)). The counter is refreshed as well when a reply is stopped or fails, and when a reply you left running by opening another conversation ends.

## Use a suggested reply

Once an agent has answered, the message box may propose what you would likely ask next, greyed out, with a `Tab` key beside it. Press `Tab` (or the right arrow, or click the `Tab` key) to put it in the box, edit it if you want, then press Enter to send it: a suggestion is never sent by itself. Start typing or press Esc to dismiss it.

There is not always one: the platform proposes nothing when the next step is not obvious, after a reply that failed or that you stopped, or when you already sent another message. The suggestion is made by the routing model your administrator sets in `Settings > AI Models`, from your message, the agent's answer and the tools it used. Turn suggestions off in [your profile](my-profile.md#profile-tab).

## Add to a reply that is still running

You do not have to wait for an answer to correct or complete your request. The composer stays open while the agent works:

1. Type your message, and attach files if you need to.
2. Press Enter, or the send button (`Send now: the agent folds it in at its next step`). The line under the composer reads `Enter to send now · Esc to stop generating`.

Your message appears in the conversation at once, and the agent takes it into account at its next step, between two tool calls, in the same answer: you get one answer that covers both messages, not two. If the agent was waiting (for a delegated task, for example), your message ends the wait. A message that arrives while the agent is already writing its final answer is answered right after it.

- A message refused before it reaches the agent (you sent too many messages in a minute, or it could not be saved) is put back in the composer, so you can send it again.
- A message that mentions another agent, or references another conversation, is held and sent as its own message when the answer ends (see [Ask several agents at once with @mentions](#ask-several-agents-at-once-with-mentions)).
- A message you send while a reply is running can count as one more agentic action.
- To abandon the answer instead, use `Stop generating` (or Esc).

## See how full the conversation's context is

Once the assistant has answered in a conversation, a small ring and a percentage on the right of the composer show how much of the model's context window the latest answer used. Hover it for the summary (`Context used`, with the number of tokens); click it to see where the context went: `System prompt`, `Tool definitions`, `MCP & dynamic tools`, `Summarized conversation`, `Tool results` and `Conversation`. The counts are estimates.

The colour follows what happens to the conversation:

- amber, `Context nearly full: older turns will soon be summarized`: the oldest messages are about to be replaced by a summary;
- red, `Context overflowed: older content was cut to fit`: older content was cut so the answer could run.

When the gauge turns amber or red on a long conversation, starting a new conversation gives the assistant its full context back.

## Ask several agents at once with @mentions

Type `@` in the composer and pick an agent from the list to mention it. You can mention several agents in the same message.

- In a new conversation, the first agent you mention becomes the conversation's agent.
- The conversation's agent answers first. Then every other agent you mentioned answers in turn, in the order you mentioned them. Each reply is its own message, labelled with the agent's name.
- Every mentioned agent receives your full message, including attachments, and can read the replies given before its own in the same turn.
- Each agent is told which agents answer after it in the turn, so it answers its own part and does not ask you to pass your message on to them.
- An agent you mention twice answers once.
- Only agents you can chat with answer: a mention of an agent that is not available to you (not shared with you, disabled, or not open to chat) is ignored, and the others still answer. If an agent fails while answering, a short note replaces its reply and the agents after it still answer.
- Clicking stop ends the turn: the agents that have not answered yet are not asked.
- The mentioned agents of one message share a time limit. An agent still answering when it runs out is stopped, and a note names the agents that were not asked.
- Each agent that answers counts as one agentic action, charged before it starts, exactly as if you had asked it alone; an agent it consults while answering adds nothing. If your agentic quota runs out, the agent that could not be charged says so and the agents after it are not asked. An agent that fails is credited back like any failed answer.
- Each reply keeps what its own agent produced: its trace (for feedback) and the files it generated.
- A message you send while an answer is still being written is held and sent as its own message when the answer ends if it mentions another agent, or while several mentioned agents are answering. A message that reaches a running answer anyway (from another device, or through the API) goes to the agent already answering, and the other agents it mentions then answer it, after the agents of the running turn and under the same rules. One that arrives as the answer is ending is answered by them after the next answer in the conversation.

Once an administrator turns them on (`CHANNEL_AGENT_MENTIONS_ENABLED=true`, see [Configuration](../deployment/configuration.md#messaging-channels)), mentions also work in messaging channels (Slack, Teams, WhatsApp, Telegram, Discord and Google Chat), for people with an XTM One account. Until then, a channel message is answered by its routed agent alone, whatever it mentions. There is no picker there: type the agent's key after `@`, the same key the web composer inserts, such as `@mail-desk`. A mentioned agent answers only when its own channel settings let it answer that message: it is enabled, open to chat, set to **Receive** on that channel (and on that channel conversation, if it is restricted to some), past the bot's receive whitelist, the private-conversation and only-mention rules, and set to **Publish** there. With mentions turned on, an administrator decides which agents can answer on a channel in each agent's Channels settings. Any other `@` word - a person, a handle, an agent not set up on that channel - is plain text: the routed agent answers alone, as it would without the mention, and no note is added. Anonymous senders cannot mention agents. Each agent that answers counts as one agentic action, as in the web chat, and is told which agents answer after it. When the routed agent has nothing to add, the agents you named still answer. The replies arrive together in one bot message, one section per agent, with a link to every file an agent produced, and are stored as separate messages in the conversation, each with its own agent's files.

## Point the agent at another conversation with @

To have the agent read another conversation - "@q3-threat-report, what did we decide there?" - type `@` in the composer: below the agents, `Reference a conversation` lists the conversations you can open, the most recent first. Type words of a title to narrow the list, joined with hyphens (`@q3-threat` finds "Q3 threat report"), then pick one: the composer inserts its key, the title in lowercase with hyphens, such as `@q3-threat-report`.

- The list holds your own conversations, from the web chat and from the assistant embedded in OpenCTI or OpenAEV, and those shared with you by name. It leaves out archived and empty conversations, the conversation you are writing in, and conversations only reachable through an organisation link.
- Only a conversation you picked from the list counts: an `@` word you type by hand stays plain text. Deleting the key from your message removes the reference.
- With your message the agent receives a short brief of each conversation you referenced: its title, the summary and the instructions kept from its earlier messages, and its last few messages. Every agent that answers the message reads it, the mentioned ones included.
- For the rest of the conversation, the agent can read the referenced conversations in full, page by page. It can read only the conversations referenced in the conversation it answers, and only while you can still open them: one deleted or no longer shared with you is no longer readable.
- Your message shows a chip for each conversation it referenced. Clicking it opens that conversation. Someone you share your conversation with sees the chips, which name the referenced conversations, but can open only the ones they have access to.
- A message references at most 5 conversations.
- A message that references a conversation while an answer is still being written is held and sent when the answer ends: a correction sent to a running answer cannot carry a reference.

The Presentation Studio's composer offers the same list, with conversations only. In the assistant embedded in OpenCTI or OpenAEV, the list appears once the platform enables it.

## Ask for changes to a file the agent produced

When an agent hands you a file — an HTML briefing, a CSV export, a Markdown report, a JSON bundle — you can ask for a change in plain words: "remove the paragraph about X", "rename the column to Y", "fix the date in the footer". The agent reads the file it produced, applies exactly the change you asked for and returns a **new revision** of the file. The version you already downloaded is never altered under you.

- Text-based files (HTML, Markdown, CSV, JSON, YAML, source code, ...) are edited in place, so a one-line change stays a one-line change — the rest of the document is untouched.
- Rendered documents (PDF, Word) have no editable source: the agent regenerates them from the corrected content.
- Files you uploaded yourself can be corrected the same way; the result is a new file next to your original.

!!! tip
    Name what you want changed as it appears in the document. The agent matches your words against the file's exact text, so "remove the sentence about JP" works when the document says "JP"; a paraphrase may need one round of clarification.

### How a reply shows its files

- The files an answer names are shown as cards in the answer. The other files the agent wrote while working on it (a first draft, a scratch file) are shown as compact chips, still downloadable.
- An answer that names none of its files shows them all as cards.
- Files that share a name and type in a conversation are numbered `v1`, `v2`, ... in the order they were written, and their cards show the exact size in bytes. Every card shows the time its file was written; hover a card to see its file id and the start of its SHA-256 digest.

## Use prompt templates

If prompts are available in your environment, the prompt button opens a list of reusable prompt templates.

Use this when you want to:

- start from a standard request
- keep your wording consistent
- save time on recurring questions

Selecting a prompt inserts its content into your message box.

## Use session tools

The wrench button opens the session tools menu. It has one sub-menu per kind of tool, and each sub-menu lists its tools with a switch. Depending on the conversation, you may be able to switch on or off:

- built-in tools
- custom tools
- integrations
- MCP servers
- knowledge bases

Each kind shows how many of its tools are on. Switching a tool keeps the menu open, so you can change several in a row. The built-in tools of the Platform Health specialists (Memory Steward, Health Diagnosis, Health Experiments, Health Flow Lab) are Platform Health's own: the menu never lists them, and `Disable all` leaves them as the agent carries them.

When you use the `General Assistant`, this menu lets you build a temporary toolset for the conversation. When you use a specific agent, the panel starts from that agent's own toolset and lets you switch built-in tools on or off for the conversation. Work items, shared state and trace inspection act as the agent itself, so they are only offered when the agent already has them or when you can configure the agent. Trace inspection reads the agent's traces, which hold every user's conversations with it, so the one you add, and the one the platform builder (the suite named after the platform) brings along, only work for someone who can read the agent's chats: an admin, its owner, or someone it is shared with as `Full access (incl. chats)`.

A message can only add to the conversation what you could connect to an agent of your own: one that adds a custom tool, MCP server, knowledge base or integration you cannot see is refused with an error that says which kind it is, and nothing is sent. Saving your profile's default tools follows the same rule. What the conversation already has - the tools you selected earlier, your profile defaults, the agent's own toolset and the General Assistant's configuration - is used as it was set up, including what the owner of an agent shared with you connected to it.

Integrations are the one exception, as they always were: an integration you cannot see, or whose owner's connected account is not shared with you, is never used for you in a chat (see [Sharing an OAuth integration](resources/integrations.md#sharing-an-oauth-integration)). Nothing is hidden: when an answer could not use one, a note under it names the integration - by its service only when it is not shared with you - and says why, and the agent is told as well, so it can say what it cannot do. Connect your own integration of that service, or ask the owner to share theirs.

The menu also lets you:

- reset back to the default configuration
- enable all visible tools (the ones already on stay on)
- disable all visible tools
- hide integrations from the menu and show them again (`Show or hide integrations`, at the bottom of the `Integrations` sub-menu)
- pick the platform an integration scoped to several registered platforms works on (its own sub-menu)

## Voice options

If your browser supports speech features, Chat can offer two voice modes.

- `Dictate a message`, the microphone in the message box, converts your speech into text.
- `Voice call` opens a spoken conversation: you speak, the agent answers aloud, and you can interrupt it.

If these controls do not appear, your browser or device does not currently support them.

## What you may see above the messages

The conversation header can show extra context.

You may see:

- the active agent name
- the current channel, such as web chat
- a transfer banner if another agent handed the conversation over
- a `Return` action if you want to go back to the source agent

This is especially useful when the conversation moved from one agent to another.

## A conversation open in several places

When the same conversation is open in several tabs, on another device, or in voice mode, each one shows the answer as it is written, wherever the message was sent from. Voice mode reads out only the answer to what you said.

When XTM One is updated while a tab stays open, that tab shows `A new version is available`: select `Reload` to use the new version. Until you do, the tab keeps working, but it may show an answer started elsewhere as if you had asked it.

To connect your own scripts to a conversation, see the [Chat API](chat-api.md).

## Continue a conversation in Slack or Teams, or back on the web

A conversation is not tied to the place it started. The same conversation - its history, files, session tools and agent - can go on in the web app, in a direct message with the Slack bot, or in your personal chat with the Microsoft Teams bot.

- **From Slack or Teams to the web.** A direct-message conversation you had with the bot appears in your conversation list, marked with the Slack or Teams icon. Open it and write: the agent answers here, with everything said in Slack or Teams. Each message in the transcript shows where it was written.
- **From the web to Slack or Teams.** Open the conversation, then `Conversation options` > `Continue in Slack or Teams`, and pick the app. The bot sends you a direct message: in Slack, reply in its thread; in Teams, reply in your chat with the bot. The conversation goes on there, and stays in your list here.
- **Each reply is written where you send your message**, with that app's formatting. A message you send in Slack is answered in Slack; one you send here is answered here.
- **The same agent answers everywhere.** When your administrator linked the General Assistant to an agent (Settings > General Assistant > `Link to Agent`), a Slack or Teams conversation with the General Assistant is answered by that agent there and here alike: the chat shows its name, and its tools in the composer. A conversation you started here with the General Assistant before the link keeps the General Assistant in Slack or Teams too.
- **One answer at a time.** A message you send in Slack or Teams while another reply in the conversation is being written (here or there) is queued: the bot tells you so right away, and answers it as soon as that reply is done. Several messages you send meanwhile in the same thread or chat are answered together, in one reply, as the web does with a message sent while the agent is still working. Up to five messages can wait; past that the bot asks you to send yours again later. A queued message the bot could not answer - within an hour, or because something failed twice - is dropped, and the bot tells you so in the thread so you can send it again. A queued message keeps the files you sent with it. If the thread stops continuing the conversation before a queued message runs (you send `/new` or stop the continuation), that message is not answered, and the bot says so. While a reply to a message sent from Slack or Teams is being written, the web refuses a new message and asks you to send it again once the reply arrives.
- **Tool calls waiting for your approval are answered in the web app.** Slack and Teams cannot show the proposal. While the turn that proposed the call still waits for your decision, a message you send in Slack or Teams is queued behind it. If that turn ended with the call still waiting, a new message moves the conversation on, as it does on the web: the call is dropped, and the reply starts by saying so.

A few limits:

- Only the person who owns the conversation can continue it. A conversation shared with you stays read-only, and a conversation from someone who wrote to the bot without an XTM One account is never listed.
- Group threads stay where they are. A Slack channel thread or a Teams group chat or channel holds other people's messages, and everyone in it reads the agent's answers, so it is not listed here and is continued in Slack or Teams only: you cannot send a message or upload a file into one from here.
- In Teams, your personal chat with the bot has no threads: once you continue a conversation there, every message you send in that chat continues it, until you send `/new` or continue another conversation there. Teams also needs you to have written to the bot once, so the bot can write back to you.
- The option is offered only where the bot would answer you: the conversation's agent must answer on Slack or Teams (a conversation with the General Assistant always can), and the bot must take direct messages. A Slack bot an administrator restricted to some channels with direct messages turned off does not, and the dialog says so.
- `Stop` in the same dialog ends the continuation: the next message you send in that Slack thread or Teams chat starts a new conversation.

## Export a conversation

`Conversation options` > `Export as Markdown` or `Export as PDF` downloads the conversation as a document: every message in order, under who wrote it and when. A chart an answer shows (see [Platform insights](platform-insights.md)) is written as a table of its values. Anyone who can read the conversation can export it, a shared one included; the 2,000 most recent messages are exported.

## Share a conversation

To let colleagues read one of your conversations, open `Conversation options` > `Share`. The `Share conversation` dialog gives read access in two ways:

- **People and groups.** Type at least two characters of a name in `Search people and groups…` and pick a person or a group. Each one you add is listed as `Can view`; the bin icon removes their access at once. The conversation then appears in their sidebar, under `Shared with me`.
- **Anyone with the link.** Turn on `Anyone on the platform with the link can view` to paste the link in a channel or a ticket without listing recipients first. Readers still have to sign in to XTM One, and the conversation does not appear in anyone's sidebar: it is reached through the link only.

`Copy link` copies the conversation's address. It is always available: someone who has no access yet simply cannot open it until you add them.

A shared conversation is read-only for the people you share it with: they can read it and download its files, but cannot reply in it, rename it or delete it. Only the owner can share a conversation.

!!! warning
    A conversation can hold results the agent retrieved from integrations or knowledge bases. Sharing it shares those results too, whether or not the people you share it with have access to the source.

## Rate an answer

Point at an answer to show the thumbs under it: `Helpful response` or `Not helpful`. A thumbs down opens a dialog where you can say what went wrong; when your administrators defined feedback criteria (`Settings > Quality`), the dialog also asks you to score them, for a thumbs up too. Click the thumb you chose again to remove your rating.

Your ratings reach the people who manage the agent, in the agent's `Feedback` tab. After a reload, an answer you already rated shows its thumbs greyed out (`Feedback already submitted`), without saying which one you chose.

## Find the files of a conversation

`Conversation options` > `Conversation files` opens a panel listing every file of the conversation: the files you attached (`Uploaded`) and the files the agent produced (`Generated by agent`), the most recent first. Filter them with `All`, `Uploaded` or `Generated`. For each file you can:

- `Download` it;
- `Show in conversation`, which scrolls to the message that carries it;
- `Add to the workspace's sources`, when the conversation is filed in a workspace you manage (see [Workspaces](workspaces.md)).

`Open in Files` opens the [Files](files.md) page on this conversation's files. People you shared the conversation with see the same panel.

### When an administrator opens someone else's files

An administrator who opens the files of a conversation that belongs to another user first sees a confidentiality notice, `You are about to open someone else's files`, before any file name is shown. To continue, they confirm `I have a legitimate need to view these files and I understand this access is logged.` and select `View files`. The access is recorded in the audit log, with the conversation and its owner, and lasts 15 minutes; while the panel is open, a reminder says the files belong to another user. `Cancel` closes the notice without showing anything; `Review notice` brings it back.

## Background task banners

Some requests continue in the background.

When that happens, Chat can show:

- a banner while background tasks are still running
- a completion banner when those background tasks finish
- a failed count if some delegated tasks did not complete successfully

This means the request involved delegation or longer-running work. It is normal behavior, not an error by itself.

Files a delegated task produces (a CSV export, a spreadsheet, a report) belong to the conversation: they appear as download chips on the message the agent posts once the task finishes, and the agent itself can open, check and merge them with its own file tools when it summarizes the result.

Work the agent hands to the background in the Video Studio (a render, an AI shot, a recording, AI music) is shown the same way once the reply ends: a banner says `The assistant resumes this conversation when this work finishes` and lists each job with how far along it is. When the work ends, the agent comes back on its own and tells you how it went.

## When a reply stops before it finishes

A reply can stop before its end: XTM One was restarted or updated while the agent was working, or the process running it stopped. The conversation never just goes quiet when that happens.

### The agent resumes by itself

When the platform itself stopped the reply (a deployment, a scale-in or a restart of the platform, or the recycling of a web worker that served its quota of requests), you have nothing to do: once XTM One is back, the agent resumes the same message by itself, from the tool calls it had already completed, and the conversation shows it working again.

It keeps resuming as long as the message makes progress between two stops, for up to 12 hours after the first stop. After three stops in a row that completed no new tool call, it stops resuming and waits for you.

### The status card

In place of the interrupted reply, the conversation shows one status card for the whole episode: every stop of the same message folds into it, and the card follows what happens live, in every tab and on every device. It reads, for example:

- `Interrupted by a platform restart, resuming by itself`, then `Interrupted by a platform restart, resumed by itself` once the agent answers. A message you send while it resumes goes to the running answer as a correction and does not change the card. The Presentation Studio side chat shows the same card;
- `Interrupted by a worker recycle, resumed by you`, when your own message picked the work up and no automatic resume was running;
- `Interrupted by a server shutdown, could not resume by itself`, when the agent did not resume (see below).

The chip on the card counts the tool calls the agent had completed (`3 tool calls`). Click it to see them grouped by tool: the last one completed is highlighted (`Last completed`), a failed call is counted as failed, and the calls still running when the reply stopped are counted too.

The agent's memory, when it has memory switched on, records the interrupted request and its last completed action.

### When the agent could not resume

A reply stopped for another reason (the process ran out of memory, for example), or past the limits above, waits for you. The card turns into a warning and offers two buttons to the conversation's owner:

- `Resume` sends `Continue where you left off.` for you. The tool calls the agent had already completed are kept: the agent continues from their results instead of running them again. Sending any other message continues the same way.
- `Dismiss` stops offering the resume. The card stays, as `not resumed`.

A process that crashes or is killed leaves no interrupted reply to draw a card for. The end of the conversation then shows a notice, `This response stopped before it finished`, with when it stopped and how many completed tool calls are kept, and the same `Resume` and `Dismiss` buttons. It stays there after a reload and on your other devices, until a new reply starts. When the reply is already queued to resume by itself, the notice says `It is resuming by itself: no need to click anything.` and offers no `Resume`.

If XTM One cannot be reached while you are on the page, the chat keeps trying to reconnect for as long as the page is open, and follows the agent again as soon as it is back. A message XTM One refuses before any reply starts (you sent too many messages in a minute, for example) is never dropped silently: the reason shows in place of the reply.

## The waiting arcade

While an agent works through a longer request, a small Space Invader appears under the conversation and shoots the waiting message away, letter by letter. It is more than an animation: click it, or its `Take control` button, and a full Space Invaders game opens over the whole interface. You can also start a game at any time, no wait needed: `Play Space Invaders` in your user menu (top right), or the command of the same name in the command palette.

The chat embedded in OpenCTI, OpenAEV and OpenCRQ invites you here too: while its agent works, its `Play Space Invaders in XTM One` link opens `<your XTM One address>/chat?arcade=play` in a new tab, and the game starts as soon as you are signed in - even if you have to sign in first, or wait for an administrator to accept the platform's Terms of Use, as long as that happens within ten minutes of the click.

- **Controls**: arrow keys or `A`/`D` (`Q`/`D` on an AZERTY keyboard) to move, `Space` to fire (hold it for autofire), right-click or `X` to send a missile where you aim, `P` to pause, `M` to mute, `Esc` to leave (the first press pauses, the second ends the game and saves it). On a touch screen, drag to move and fire, and tap the rocket for a missile.
- **Waves and bosses**: every wave marches faster than the last, and every fifth wave brings a boss. Falling capsules hold power-ups: rapid fire, spread shot, laser, shield, extra missiles, and a few rarer ones.
- **The interface is fair game**: your shots and the invaders' leave holes in whatever they hit - buttons, messages, the sidebar. Nothing is really broken: the damage is cosmetic, and everything is rebuilt the moment you leave. Your conversation keeps running underneath.
- **Your answer comes first**: when the agent has finished answering, a banner says so - a message it posts along the way before carrying on with its work does not count. `Esc` (or `Read it`) takes you straight back to it, and if you had stopped playing, the arcade takes you back by itself after a few seconds. `Keep playing` lets you finish your game. The game waits for the conversation you started it from: if you switch to another conversation or leave the chat while you play, the game goes on but no longer announces that answer.

### Leaderboard

Every game you play is scored and saved. The leaderboard - `Arcade leaderboard` in your user menu, the trophy button next to the waiting game, the `Open the leaderboard` command, or the pause menu - ranks the best game of each player over the last 24 hours, the last 7 days and all time, shows who is playing right now, and names the holders of three titles: top vandal, sharpshooter and marathon. During a game, the score bar shows whose score you are about to beat.

- Your **callsign** is three letters or digits, like on a real arcade cabinet. It defaults to your initials; change it in the leaderboard's `My arcade` tab. Other players only ever see your callsign - never your name or your email.
- Turn off `Show me on the public leaderboard` to keep your scores, titles and live games to yourself, from that moment on - even in a game already running. You still see your own rank.
- Scores are checked by the server: a score, a number of shots or a count of destroyed interface elements that is not plausible for the time played is refused, and so is a score higher than the waves you reached can yield. The arcade tells you why a score was not recorded. A game in which a cheat code was used is saved but not ranked.

### Achievements and secrets

The leaderboard's `Achievements` tab lists what you have unlocked. Many achievements are secret until you find them: old cheat codes, a very rare saucer, certain dates on the calendar, and a few things you probably should not shoot. Explore.

### When the game is not offered

The gamepad always means play: on the waiting panel it opens the game, and nothing in the chat hides it. When your system asks for reduced motion, the waiting panel shows a plain rotating message instead of the invader and the chat offers no game; `Play Space Invaders` (in your user menu or the command palette) and a `?arcade=play` link still start one, since you asked for it. The arcade is not offered while an administrator impersonates a user: nothing is ever recorded under someone else's name. Nor is it offered while the platform's Terms of Use are waiting to be accepted. Administrators can turn the arcade off for the whole platform with `ARCADE_ENABLED` (see [Configuration](../deployment/configuration.md#waiting-arcade)).

The game and the leaderboard load in the background shortly after you sign in, so a page left open while XTM One is updated can still play. If they could not load before the update (on a Data Saver or 2G connection, for instance), opening them shows `Reload to play` or `Reload to open the leaderboard`: `Reload` reloads the page and opens the game or the leaderboard right away.

## When an agent is shown as not recommended for chat

Some agents are built mainly for background execution rather than direct conversation.

These agents still appear in the picker, but XTM One marks them so you know they are not optimized for normal chat use.

You can still try them, but the best experience usually comes from:

- the `General Assistant`
- a chat-enabled agent

## Practical tips

- Start with the `General Assistant` if you are unsure which agent to use.
- Use a dedicated agent when the topic belongs to a specific workflow.
- Attach documents when the answer depends on exact wording or evidence.
- Open the same conversation again if you want XTM One to keep the discussion context.

## Next step

The next chapter explains [Agents](agents.md), including what the list page shows and how to read an agent detail page.
