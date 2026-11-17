# Data model

XTM One is easier to understand when you treat it as a set of connected objects rather than a set of disconnected screens. The same object can appear in [`Chat`](../chat.md), the [`Agentic Flow`](../agentic-flow.md) graph, [`Operations`](../work-history.md), and the [`AI Catalog`](../ai-catalog.md) without becoming four different things.

At a high level there are three kinds of things: assistants that do work, resources those assistants rely on, and records that show what happened.

## Assistants and records

- **[Agents](../agents.md)** are the main assistants. An agent bundles its instructions and persona, tools, knowledge bases, prompts and skills, assignments, handovers, and memory. If you remember one object type, remember this one.
- **Assignments** are automation rules inside an agent — they define what triggers the work and what the agent does when it runs. They are not separate assistants.
- **Runs** are the execution records created when assignments or other background work happen. They tell you whether the work ran, is still running, failed, is waiting for a person, or what it produced — the operational layer of the product.
- **Conversations** are the chat sessions between a user and the `General Assistant` or a specific agent. The conversation holds the discussion; the agent stays the reusable assistant behind it.
- **Fleets** are named groups of agents that work together: an orchestrator and the agents that report to it. An agent belongs to at most one fleet (its home); a fleet can also connect agents or other fleets it calls without owning them. Every fleet coordinates in exactly one workspace (its work items, shared state, worksets and knowledge), and publishes a fleet card: what it does, its entry agent and the agents other fleets may hand over to. A fleet shows its handover graph (including the handovers that cross into or out of it, and whether a connected link governs each one), its declared hierarchy, its schedule, its recent activity and the health of each member; agents in no fleet form the playground. An agent in no fleet works exactly as before. Removing an agent from a fleet, or deleting the fleet, never deletes the agent nor changes its handovers.

## Reusable resources

The [`Resources`](../resources/index.md) area holds the building blocks agents and chats depend on: knowledge bases (searchable documents), prompts (reusable instructions), skills (reusable instruction packages), MCP servers (external tools over the Model Context Protocol), tools (built-in or custom actions), and integrations (connections to outside systems).

## Access and sharing

Users have personal settings and access; groups are used for controlled sharing. Most objects have one visibility state — personal, shared with a group, or company-managed — which decides who can edit them and whether you should reuse an object or copy it.

## How it appears in the UI

Most areas follow the same pattern: a list page, a detail page, and tabs for overview, configuration, activity, and related records. The UI is not inventing a new concept on every screen — it shows different views of the same underlying objects. In practice they chain together: a user opens an agent, the agent contains assignments and references resources, running it creates a run in `History`, and chatting with it is stored in a conversation.

## Next step

Continue with [`Relationships and nesting`](relationships-and-nesting.md), which explains how these objects point to one another and where configuration actually lives.
