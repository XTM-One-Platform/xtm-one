# Office add-in

The Office add-in brings XTM One chat into Microsoft Word, Excel, PowerPoint and Outlook. It opens as a side panel next to your document or email: you pick an agent, ask about what you have open, and put the agent's reply into the document with one click.

## What the add-in does

- **Chat with your agents** from inside Office, with the same agents and the same conversations as the web app.
- **Send what you have open** with each message: your selection and part of the document or email around it, so you do not have to copy and paste.
- **Run quick actions** such as `Summarize`, `Translate` or `Improve` without writing a prompt.
- **Put a reply into the document**: replace your selection, add the text to the document, or add it as a comment.

The add-in is part of the platform: there is nothing else to deploy. Your platform serves it at its own address followed by `/office/`.

!!! note "Platforms served under a path prefix"
    The add-in only loads from a platform served at the root of its host. On a platform served under a path prefix it does not load. See [Serving under a path prefix](../deployment/installation.md#serving-under-a-path-prefix).

## Install the add-in

An administrator installs the add-in for the organization, or you install it for yourself to try it. Office installs an add-in from a manifest file, and the add-in has two:

| Manifest | Adds the button to |
| --- | --- |
| `manifest.prod.xml` | Word, Excel and PowerPoint |
| `manifest.outlook.xml` | Outlook |

Both are in the `apps/microsoft-office/manifest` folder of the XTM One source code. The platform does not offer them for download.

1. Open the manifest in a text editor and replace every `COPILOT_URL` with the address of your platform, for example `https://xtm-one.example.com`.
2. Upload it to Office:
    - **For the whole organization:** in the Microsoft 365 admin center, `Settings` > `Integrated apps`, upload the manifest as a custom app. For Outlook, choose `Deploy add-in`, then `Upload custom app` with the manifest file, and assign it to the users or groups who need it.
    - **For yourself, in Word on the web:** `Insert` > `Office Add-ins` > `Upload My Add-in`.
    - **For yourself, in Outlook on the web:** the settings gear > `Add-ins` > `My add-ins` > `Custom add-ins` > `Add a custom add-in` > `Add from file`.

The button carries the name `XTM One`. To give it your platform's own name, the manifests can be generated again with that name: the `README.md` of the `apps/microsoft-office` folder explains how.

!!! note "What the add-in loads"
    The add-in's panel loads Microsoft's Office library from Microsoft and its typefaces from Google Fonts. If your network or proxy filters outbound traffic, see [Security headers](../deployment/security-headers.md) for the sources it uses.

## Open the add-in

- **Word, Excel and PowerPoint:** select the `XTM One` button on the `Home` tab.
- **Outlook:** the button appears in the ribbon when you read an email, when you write or reply to one, and when you organize a meeting.

!!! tip "The button is not in the ribbon"
    Office collapses add-in buttons when the window is narrow. Look in the overflow menu (`...`) at the right end of the ribbon. In Outlook, a newly deployed add-in can take a few minutes to appear and may need Outlook to restart.

## Sign in

The panel shows your platform's name and `Office Add-in`. If the add-in finds a session of the web app it can reuse, it opens directly on the list of agents. Otherwise it asks you to sign in with your `Email` and `Password`, then `Sign In`.

The form takes an email and a password only. An account that uses two-factor authentication cannot sign in through it: the add-in says so.

To sign out, select the sign-out icon at the top right of the agent list.

## Choose an agent

After you sign in, the panel lists who you can talk to under `Select an Agent`:

- The `General Assistant`, always first.
- `Recent`: the agents you already have a conversation with in the add-in, marked `Resume`. Select one to pick up where you left off.
- Every agent you can see: your own, the ones shared with you, and the company-managed ones, marked `Shared`. An agent that is disabled, or set not to chat, is not listed.

Type in `Search agents...` to filter the list by name or description. The refresh icon at the top reloads the list, and the sun or moon icon switches between the dark and the light theme.

## Chat about your document

Once you choose an agent, the panel opens its conversation. Type your question in `Ask about your Word document...` (the host's name changes with the application), then press Enter or the send button. Shift+Enter starts a new line.

- When you have something selected, a strip above the messages shows the start of your selection, so you know what goes with your message.
- `+` at the top starts a new conversation with the same agent.
- The back arrow returns to the agent list. The next time you choose that agent, the add-in starts a new conversation.

The add-in's conversations are ordinary XTM One conversations. When the same conversation is open in the web app too, a message sent there shows up in the panel, and on a turn that several agents answer, each reply is labelled with the agent that wrote it, as in the web chat. See [A conversation open in several places](chat.md#a-conversation-open-in-several-places).

## What the agent receives

Each message carries what you have open, not the whole document:

| Application | What goes with your message |
| --- | --- |
| Word | Your selection, the beginning of the document, its headings, and its word and table counts |
| Excel | The selected cells, the first rows of the active sheet, and the names of the workbook's sheets |
| PowerPoint | Your selection and the number of slides |
| Outlook | The email's subject, sender and recipients, and the beginning of its body |

In Word, a message that asks for a summary or about the whole document (for example "summarize this" or "review the entire document") sends the full text of the document instead. A long document is cut after its first 12,000 characters.

!!! note "The context is part of your message"
    The panel shows only what you typed, but the message stored in the conversation holds this context too. When you open the conversation in the web app, or reopen it in the panel, you see your message with the document content that went with it.

## Quick actions

Select the sparkle icon at the left of the message box to open the quick actions. The first four also appear as buttons in an empty conversation. Each one sends a ready-made request about your selection or your document:

| Application | Quick actions |
| --- | --- |
| Word | `Interpret`, `Summarize`, `Translate`, `Modify`, `Improve`, `Comment`, `Review Doc`, `Insert Table`, `Insert HTML` |
| Excel | `Interpret`, `Summarize`, `Translate`, `Modify`, `Formula`, `Comment`, `Insert Table` |
| PowerPoint | `Interpret`, `Summarize`, `Translate`, `Modify`, `Improve`, `Insert HTML` |
| Outlook | `Summarize`, `Interpret`, `Draft Reply`, `Improve`, `Translate` |

In Word, `Interpret` and `Summarize` work on the whole document when nothing is selected, and `Review Doc` asks the agent for review comments on the whole document, section by section.

## Put a reply into your document

Under each reply, buttons put it into what you have open:

| Button | Word | Excel | PowerPoint | Outlook |
| --- | --- | --- | --- | --- |
| `Replace` | Replaces your selection | Writes into the selected cells, one line per row and tab-separated columns | Replaces the selected text | Replaces the whole body of the email you are writing |
| `Insert below` | Adds the text at the end of the document | Changes nothing | Adds a new slide with the text | Inserts the text at your cursor in the email you are writing |
| `Comment` | Adds a comment on your selection, or on the first paragraph when nothing is selected | Adds a comment on the selected cells | Not shown | Not shown |

The `×` button dismisses the buttons. Once a reply is applied, it shows `Applied to document`.

!!! note "The text goes in as plain text"
    The buttons insert the reply's text. Formatting in the reply, such as bold or a table, arrives as its raw characters, not as formatted content. In Outlook, the buttons only change an email you are writing, never one you are reading.

## Approvals

The add-in cannot show a tool approval. When an agent needs your approval before running a tool, it stops and explains why in its reply instead of acting. To allow the tool, or for how approvals work in clients such as this one, see [Chat approvals in client applications](../deployment/chat-approvals-in-clients.md).

## When something goes wrong

| What you see | What to do |
| --- | --- |
| `Reconnecting...` under the messages | The add-in lost its connection and tries again on its own, up to three times. |
| `Connection lost — please refresh` | Close the panel and open it again. |
| `Not connected — please wait or refresh` | Wait a moment for the connection, then send your message again. |
| `Session expired` | Close the panel, open it again, and sign in. |
| `No agents available` | No agent you can see is enabled for chat, and the list, `General Assistant` included, stays empty. Ask an administrator to share an agent with you. |

## Next step

To learn what you can do in the full chat, including files, workspaces and @mentions, see [`Chat`](chat.md).
