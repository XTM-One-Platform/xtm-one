# Containers and scopes

Some pages in XTM One are about one specific object. Others are about a container that groups many objects, or a scope that sets a visibility boundary.

## Containers

A container is any object or view whose main role is to hold and organize related work. It usually shows the items it contains and lets you add or remove them. The main ones:

- **Conversations** keep the messages, context, and history of one chat thread together.
- **Workspaces** group conversations on one topic, with the work items and shared state their agents build there and the knowledge bases the work relies on (see [Workspaces](../workspaces.md)).
- **Knowledge bases** group documents and import sources into one searchable corpus.
- **AI Catalog entries** group shareable resources into published packages that other users can review and clone.

Being inside a container does not always mean ownership: a prompt attached to an agent is still reusable elsewhere, and a catalog item added to your account usually becomes a new local copy rather than a live link to the source.

## Scopes

A scope is the boundary that decides where an object is visible and who can edit it:

- **Personal** — belongs to one user; the safest place to experiment, since edits stay local.
- **Shared** — visible to the groups or people it is shared with. Each share has a permission: `Can configure` lets them edit it too, `Full access (incl. chats)` (agents only) also lets them read the agent's conversations and its traces (its `Observability` tab, and trace inspection in a conversation or a run they trigger, even when the agent carries it itself), which hold those conversations, and `Read only` lets them look but not change it; a read-only share shows a `Read-only` badge and says so instead of offering an `Edit` button.
- **Company-managed** — visible to everyone but usually edited only by admins; the broadest scope in day-to-day use.

## Before you change something

Ask whether you are editing the container itself or an item inside it, and whether the change affects only you, your group, or the whole company. Those two questions prevent most accidental changes.

## Next step

Continue with [`Reuse and deduplication`](deduplication.md), which explains when XTM One keeps a shared object, when it creates a copy, and how to avoid duplicate setup.
