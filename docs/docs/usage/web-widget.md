# Publish an agent on a website

You can put an agent on any website as a chat widget: a floating button in the bottom-right corner of the page that opens a chat panel. Your visitors talk to the agent without an XTM One account and without signing in.

This page covers how to turn the widget on, where to get the embed code, what visitors see, what the agent can do for them, and the limits that apply to anonymous visitors.

## Before you start

- You need to be able to edit the agent: you own it, it is shared with you with configuration access, or you are an administrator. On a company-managed agent, only administrators can change it.
- The widget answers only where chat is available. On Community Edition, or when an administrator has turned AI off, visitors get an error instead of an answer. See [Editions and licensing](../administration/editions-and-licensing.md#what-community-edition-looks-like).

!!! warning "Anyone can reach the agent"
    Once the widget is on, anyone who has the embed code, or opens a page that carries it, can chat with the agent. The agent's persona and knowledge bases answer people you do not know. Expose only an agent whose knowledge you are willing to make public.

## Turn on web exposure

1. Open the agent, then `Configuration` > `Channels & Bots`.
2. In the `Web Exposure` card, marked `Danger Zone`, turn on `Expose on Web`.
3. If XTM One shows what the change lends to new people, read it and select `Enable` to go on. See [Sharing and access](resources/sharing-and-access.md#when-you-are-asked).

The card then shows a `Live` badge, and `Get Embed Code` appears for the people who can edit the agent: the embed code chats as the agent's owner, so the people it is shared with read-only see the widget is live, not its code. On the `Agents` list, the agent carries the `Web Exposure` indicator, and you can filter the list on it.

Turn `Expose on Web` off to stop the widget on every site that uses it. The embed code does not change: when you turn the switch back on, the pages that still carry the old code work again.

!!! note "Copies start without the widget"
    When you duplicate, clone or export an agent, the copy has web exposure and visitor uploads turned off, and gets its own embed code once you turn them on.

## Get the embed code

1. Select `Get Embed Code`. The `Embed Code` dialog opens.
2. Select `Copy` to copy the snippet.
3. Paste it into your website's HTML, just before the closing `</body>` tag.

The snippet carries two attributes:

- `data-token` is required. It identifies the agent, and it is the only key to it: treat it like a password you chose to publish.
- `data-context` is optional. It is a JSON object that describes the page the widget is on, such as the page, the product, the section or the role of the user. The widget sends it with every message, so the agent can adapt its answer. Replace the example values with your own, or remove the attribute.

!!! tip "Keep secrets out of the page context"
    `data-context` is written in your page, so every visitor can read it and change it. Use it for hints, never for anything private or for a decision the agent must trust.

There is no list of allowed sites: the widget works on any page that carries its embed code.

## Add suggested prompts

Suggested prompts are the clickable conversation starters the widget shows under its greeting. They appear in the `Web Exposure` card once the widget is on.

- Type a prompt in `Add a suggested prompt…` and press Enter, or select the plus button.
- Select `Generate with AI` to have prompts proposed for you.
- Remove a prompt with the cross on it.
- Select `Save Prompts` to publish your changes, or `Cancel` to drop them.

## What visitors see

The widget shows a button in the bottom-right corner of the page. Selecting it opens a panel with:

- the agent's name, and a greeting with its description and your suggested prompts;
- `New conversation`, to start over, and `Maximize` / `Restore default size`, to change the panel's size;
- a box that reads `Ask a question…`;
- the reminder that AI can make mistakes, and a "Powered by" line with your platform's name.

The widget's own text is in English. Answers stream as the agent writes them.

A visitor who comes back with the same browser finds their last conversation again, and an answer still being written resumes. Clearing the browser's site data, or selecting `New conversation`, starts a new one.

## What the agent can do on the widget

Conversations use the agent's persona and its knowledge bases. A widget conversation is more limited than a chat in XTM One:

- **No integrations, MCP servers or custom tools.** The agent cannot act with the tools and credentials it uses elsewhere, so a visitor can never drive them.
- **No approvals.** A visitor cannot approve a tool call. If the agent requires approval for a tool, the turn stops with an error instead of waiting.
- **No downloadable files.** The agent cannot generate files for the visitor; file generation stays available in authenticated chat.
- **No agent memory.** What the agent remembers from its other conversations is never used on the widget.

A message that looks like an attempt to take control of the agent is refused with a polite answer. If an administrator has connected a prompt guard, it can also check visitors' messages and the agent's answers. See [Configuration](../deployment/configuration.md#security-feature-flags).

## Let visitors attach files

Visitors can only type until you allow uploads. In the `Web Exposure` card, turn on `Allow visitor uploads`. The widget then shows an `Attach files` button, and visitors can also paste a screenshot or drop a file on the panel.

!!! warning "Uploads are open to anyone"
    Anyone who can open a page with the widget can send files, without an account. Leave uploads off unless your visitors need them. The security audit lists every widget that allows them.

What a visitor can attach:

- **Types**: PDF, Word, Excel, PowerPoint, text and data files (such as `.txt`, `.md`, `.csv`, `.json`, `.xml`, `.yaml`, `.log`, `.html`) and images (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`). No SVG, archive, audio or video. Each file is checked against its content, not only its name.
- **Size**: 40 MB a file, and at most 10 files and 100 MB per conversation.
- **Budgets**: uploads are counted per visitor and per widget each hour. The widget as a whole also has a storage limit across all its conversations.

The agent reads a document as its text and sees an image as a picture.

When a file is refused, the widget shows the reason next to its name: its type, its size, a budget, the storage limit, or the content check. A file refused for what it is leaves the tray. A file the platform cannot take right now, because it is busy or a budget is reached, stays in the tray so the visitor can send again later.

Visitor files belong to their conversation. Only the visitor's own browser downloads them, and on the platform only administrators can open them. They are deleted after a period without new messages in the conversation, 7 days by default. A file never sent with a message is deleted after a day. Turning `Allow visitor uploads` off stops new uploads; files already attached follow the same deletion rules.

The exact rules, and the retention an administrator can change, are in [Visitor uploads on the public chat widget](../deployment/configuration.md#visitor-uploads-on-the-public-chat-widget).

## Limits for anonymous visitors

Visitors have no account, so their use is bounded per visitor address and per widget:

- 10 new conversations an hour from one address;
- 30 messages a minute from one address, across all widgets;
- 20 messages a minute from one address to the same widget.

Every widget message counts against the platform-wide agentic limits, like any other request. When those limits are reached, visitors read `This service is at capacity right now. Please try again later.` Their use is never attributed to you or to any other user. See [Requests with no account](../administration/settings.md#requests-with-no-account).

How visitor addresses are grouped, and what happens when the rate-limit store cannot be reached, are set by an administrator. See [Configuration](../deployment/configuration.md).

## Review widget conversations

Widget conversations appear in the agent's `Chats` tab, under the `Embedded Chat` channel, with the other conversations of the agent. You can read them if you own the agent, if it is shared with you with access to its chats, or if you are an administrator. See [Agents](agents.md#the-detail-page).

## When the host site has its own security policy

The widget runs inside the page of the site that embeds it. If that site sends a Content-Security-Policy, the policy must allow what the widget loads, or the widget does not appear or renders unstyled. Give your web team [The embeddable chat widget](../deployment/security-headers.md#the-embeddable-chat-widget).

An administrator can also make the widget render answers more strictly, showing only the images the platform serves. See [Configuration](../deployment/configuration.md#security-feature-flags).

## Next step

Continue with [Sharing and access](resources/sharing-and-access.md) to see everyone your agent reaches, or with [Agents](agents.md#exposing-an-agent-on-a-messaging-platform) to put the same agent on Slack, Microsoft Teams or another messaging platform.
