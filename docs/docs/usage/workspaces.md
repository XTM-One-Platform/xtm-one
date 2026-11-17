# Workspaces

A workspace is a named environment for one line of work — an audit, a customer, a research topic. You file conversations into it, and the agents working in those conversations share what they collect there: the work items they create and the shared state they keep. You can also record which knowledge bases the workspace works with.

Workspaces are an Enterprise Edition feature (see [Editions and licensing](../administration/editions-and-licensing.md)). Managing them calls no model, so they keep working when AI is switched off.

## What a workspace is for

Use a workspace when you want to:

- keep the conversations of one topic together
- let agents in those conversations work from the same work items and shared state, and stay apart from other topics
- note which knowledge bases belong to that work

A conversation that is filed in no workspace works as it always has: its agents use your own work items and shared state.

## Open Workspaces

Choose `Work > Operations` from the left sidebar, then the `Workspaces` tab. The other tabs of `Operations` — `Work Items`, `Shared State`, `Worksets` — list what agents build inside and outside workspaces (see [Work history](work-history.md)).

Each workspace is a card showing:

- its name, with a `default` badge on your default workspace and an `archived` badge on an archived one, and a badge saying when it is company-managed, shared (by you, or by whom when it is someone else's) or read-only for you
- its description
- what it holds: conversations, work items (with how many are still open), shared state, worksets and knowledge bases — or `Nothing filed here yet.` For a workspace that hosts a fleet, the work items, shared state and worksets include what the fleet's agents created that you can see, the same ones its `Operations` tab lists (see [Fleets and workspaces](#fleets-and-workspaces))
- when it was last updated

Use the search field to find a workspace by name, and `Show archived` to list archived ones too.

## Your default workspace

Everyone has exactly one default workspace, named `General`, created the first time you open the list. It is listed first. Nothing is filed in it on its own: a new conversation is in no workspace until you file it, and a conversation stays in no workspace for as long as you like.

The default workspace cannot be archived, deleted or made company-managed. You can share it. To change it, open another workspace's actions menu and choose `Make default`. Only a workspace you own, that is not archived and not company-managed, can be your default.

## Create a workspace

1. Click `New workspace`.
2. Enter a name and, if you like, a description of what it is for.
3. Click `Create`.

The workspace is yours: nobody else sees it unless it is shared with them.

## Manage a workspace

Click a workspace's name to rename it or change its description. `Open workspace` in its actions menu opens the workspace's own page: your conversations filed there on the left, its [knowledge](#give-a-workspace-its-knowledge) on the right, and `New conversation` to start one already filed in it. Renaming keeps its handle (shown under the name), so existing links keep working.

The actions menu of each card offers the following. The same actions are in the `Workspace actions` menu of each workspace in the Chat sidebar (see [Organize conversations in workspaces](chat.md#organize-conversations-in-workspaces)).

- `Make default` — see [Your default workspace](#your-default-workspace).
- `Share` — see [Share a workspace](#share-a-workspace).
- `Knowledge bases` — choose the knowledge bases this workspace works with. Binding moves and copies nothing, and grants nobody access: each knowledge base keeps its own owner and sharing, and you can only bind one you can see. Someone who opens the workspace sees only the bound knowledge bases they can see themselves.
- `Archive` / `Restore` — archiving asks for a confirmation, then hides the workspace from every picker and keeps everything it holds. To restore it, click `Show archived`, then `Restore` in its actions menu. Nothing can be filed into an archived workspace until it is restored.
- `Delete` — removes the workspace after a confirmation. Its conversations are kept, in no workspace (never moved to a default workspace), its worksets are deleted together with its instructions, its sources and the index of its conversations, and bound knowledge bases and linked skills are left untouched. A workspace that still holds work items cannot be deleted: archive it instead, or clear its work items first.

These actions are greyed out when they do not apply: the default workspace cannot be archived or deleted, and a workspace you may only read cannot be changed. When an action is greyed out because of a fleet, the menu says why under it.

### Fleets and workspaces

A [fleet](agentic-flow.md#fleets) lives in one workspace: the workspace is where the fleet's agents coordinate (work items, shared state, knowledge). The card and the page of a workspace that hosts a fleet say `Hosts fleet` followed by the fleet's name; click it to open the fleet. A fleet you cannot see is counted, not named. Three rules follow:

- a workspace that hosts a fleet cannot be deleted: move the fleet to another workspace or delete the fleet first
- a workspace that hosts a live fleet cannot be archived: archive the fleet or move it first (a workspace whose fleets are all archived can be archived)
- deleting a fleet never deletes its workspace: the workspace stays with its work items and shared state

A work item created by an agent of the fleet is filed in the fleet's workspace when the agent names no workspace and you may change that workspace; a workspace the agent names always wins. Moving an agent to another fleet later moves none of its work items. Work items the fleet's agents created before this rule were filed in the fleet's workspace on upgrade when they belong to the workspace's owner; the others stay where they were and still appear in the workspace's `Operations` tab and counts. Only the work items that are filed in a workspace stop it from being deleted.

## Give a workspace its knowledge

Open the workspace's page (`Open workspace`), or, in a conversation filed in it, `Workspace knowledge` in the `Conversation options` menu. Every agent working in a conversation of the workspace - any agent, the General Assistant, the agents it consults or delegates to, and a flow's agents wired to a workspace node - receives:

- **Instructions** — up to 8,000 characters on how agents should work there.
- **Sources** — documents the agents work with. `Manage sources` adds uploaded files, pasted text, a website and synchronised sources (Notion, Google Drive, OneDrive / SharePoint, GitHub…), with the same formats, limits and synchronisation as a knowledge base. While they fit in a share of the model's context window (see [Workspace knowledge](../administration/settings.md#workspace-knowledge)), they are given whole; beyond it, they are searched. A file attached in one of the workspace's conversations can be added from the conversation's `Conversation files` panel (`Add to the workspace's sources`).
- **Knowledge bases** — the ones bound to the workspace (`Manage`, or `Knowledge bases` in the actions menu).
- **Skills** — library skills linked to the workspace: always in the agent's catalogue, loaded when needed.
- **Conversation search** — the agents can search the conversations filed in the workspace, once each has been quiet for a while (30 minutes by default). What people wrote, the agents' answers and the text of attached files are indexed, with a summary; the results of tools never are, nor are group channel threads. A conversation unfiled, moved, archived or deleted leaves the search at once, and so does one whose owner no longer sees the workspace.

Who sees what: anyone who sees the workspace sees its instructions, its sources and the counts of its conversation search, and its agents can search every conversation filed in it. Only people who can change the workspace (`Can configure`) change its knowledge. The sources live in a knowledge base the workspace owns: it appears in the [knowledge bases](resources/knowledge-bases.md) list with a `Workspace` badge, read-only there, and is never offered for binding elsewhere. Bound knowledge bases and linked skills each keep their own sharing: someone who cannot see one does not get it, in the panel or in their agents' turns.

## File a conversation in a workspace

The quickest way is the Chat sidebar, which lists your conversations by workspace: drag a conversation onto a workspace, or use its `Move to workspace` menu. See [Organize conversations in workspaces](chat.md#organize-conversations-in-workspaces).

From the conversation itself:

1. Open the conversation in [Chat](chat.md).
2. Open the `Conversation options` menu at the top of the conversation and choose `File in a workspace`.
3. Pick a workspace, or `No workspace` to take the conversation out of the one it is in.

The change applies from the next message: from then on, agents in that conversation create and read work items and shared state in that workspace.

Only the owner of a conversation can file it, and only into a workspace they can change. Archived workspaces, and workspaces you may only read, are not offered.

Filing into a workspace other people can see asks first: its members' agents will be able to search what was written in the conversation.

## Share a workspace

A workspace follows the same scopes as other resources (see [Containers and scopes](foundations/containers-and-scopes.md#scopes)). To share one, open its actions menu and choose `Share`:

1. Switch on `Share with people and groups`.
2. Search for a person or a group and add them.
3. Choose what each one may do:
    - `Read only` — they see the workspace and what its agents built there, but cannot file conversations into it or change it.
    - `Can configure` — they can also file their own conversations into it, and change it: rename it, bind knowledge bases, share it with anyone, archive it, or delete it.
4. Click `Save`.

`Who can see this workspace` says, in words, who the settings let in; while you have unsaved changes it adds `Applies when you save.`

Admins also see `Company-managed workspace`: switched on, the workspace is visible to everyone and changed only by admins. `Restrict access` then limits it to the people and groups listed. The default workspace stays personal, so this switch is not available for it.

Only someone who can change a workspace can share it. The owner keeps it whatever a grantee does: a grantee cannot take it over or remove the owner's access, and only an admin can transfer it. Removing a person's access does not unfile their conversations: they stay filed, but their agents no longer read or write that workspace's work items and shared state, and they can move them elsewhere.

## Workspaces shared with you

A workspace shared with you appears in your list with a `Shared by` badge naming its owner, and a `Read-only` badge when you may only read it. With `Read only` you see it and what its agents built there; with `Can configure` you can also file your conversations into it, change it, share it and delete it. A company-managed workspace is visible to everyone and changed only by admins. Someone else's default workspace shared with you is not yours: it carries no `default` badge and cannot be made your default.

The same rule applies to what agents do there: an agent reads the work items and shared state of every workspace its user can see that is not archived, and writes only where that user can change the workspace.

## Let your assistant manage workspaces

The `XTM One` built-in tool (the agent builder) can do all of the above from a conversation. It is not on by default: switch it on for the conversation in the [session tools menu](chat.md#use-session-tools) (the wrench button), or have an admin add it to the [General Assistant](../administration/settings.md#general-assistant), or add it to an agent's configuration. Then you can ask, for example:

- "Create a workspace called Acme audit and move this conversation into it."
- "Which workspace is this conversation in?"
- "Move all my conversations about Acme into the Acme audit workspace."
- "Take this conversation out of its workspace."
- "Bind the Policies knowledge base to the Acme audit workspace."
- "Archive the Q2 review workspace."
- "Make Research my default workspace."

Any agent can also file the conversation you are in, or create a workspace for it, when you ask ("file this conversation in Acme audit") — no tool to switch on. Into a workspace other people can see, the move waits for your approval on the approval card, whatever the agent's approval settings, and "always" is not offered: each such move is approved on its own. Where no approval card can be shown (a Slack or Teams thread, the MCP server, a scheduled run), the assistant cannot file into a shared workspace: move the conversation from its menu in XTM One.

The assistant can list your workspaces with what each holds, find your conversations by title, workspace or unfiled ones, and file several at once — when you mention no conversation, it files the one you are in. You can refer to a workspace by its exact name. It applies exactly the same rules as the pages above: it only moves your own conversations, into workspaces you can change and that are not archived, it never binds a knowledge base you cannot see, and it refuses to archive or delete your default workspace or to delete one that still holds work items. A move of several conversations is all or nothing. Deleting a workspace cannot be undone, so the assistant is instructed to check with you first. Creating, changing and deleting a workspace through the assistant is recorded in the audit log, as it is from the page; filing a conversation is not, from the assistant or from the page. The assistant can also clear a workspace's description or color, and put its default icon back. A workspace you no longer see, by name or by id, matches none of your conversations, even those still filed in it: they are listed as being in a workspace you no longer see.

The same tools are available through the [Platform tools MCP server](resources/platform-tools-mcp-server.md). There is no current conversation there, so conversations are named by their ids.

## Next step

Continue with [Operations](work-history.md) to follow the runs, work items and shared state your agents produce.
