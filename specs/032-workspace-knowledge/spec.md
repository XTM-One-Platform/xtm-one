# Feature Specification: Workspace Knowledge — Sources, Skills, Instructions and Conversation Search

**Feature Branch**: `rds/workspace-docs-conversations-8d5263`

**Created**: 2026-10-24

**Status**: Draft

**Input**: User description: "In workspaces, attach documents the agent uses and skills that are always available to the agent. Documents are embedded in a 'Sources' knowledge base tied to the workspace. Embed the workspace's conversations in a second workspace knowledge base so the agent can search them quickly. Deleting a workspace cleans up the knowledge bases it created."

A workspace today files conversations, scopes coordination state and can be bound to existing knowledge bases, but an agent chatting in a filed conversation receives none of it: the bindings are not read by any turn. This feature makes a workspace a project in the Claude Projects / ChatGPT Projects sense: instructions, sources and skills that every agent working in it receives, plus a searchable memory of its conversations.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Attach sources the agent uses (Priority: P1)

A workspace manager adds files, pasted text, web pages, synchronised sources and conversation attachments to the workspace. Any agent answering in a conversation filed in that workspace grounds its answers on them without being told where to look.

**Why this priority**: it is the core of the request and the value every other story builds on.

**Independent Test**: add a document containing a fact found nowhere else, ask a question about that fact in a filed conversation with an agent that has no knowledge base of its own, and confirm the answer uses and cites the document.

**Acceptance Scenarios**:

1. **Given** a workspace whose sources fit the context budget (FR-008), **When** an agent answers in a filed conversation, **Then** it receives every source in full.
2. **Given** a workspace whose sources exceed the budget, **When** an agent answers, **Then** it searches the sources and cites the passages it used.
3. **Given** a conversation that is not filed in any workspace, **When** an agent answers, **Then** it receives no workspace source.
4. **Given** a source still being processed, **When** an agent answers, **Then** that source is not used and the workspace page shows it as processing.
5. **Given** a file attached in a filed conversation, **When** a manager chooses "Add to workspace", **Then** the file becomes a source of that workspace.

---

### User Story 2 - Workspace instructions and skills (Priority: P1)

A manager writes instructions for the workspace and links skills from the library. Every agent answering in a filed conversation follows the instructions and has the skills in its catalogue, in addition to its own.

**Why this priority**: same delivery slice as Story 1; together they make the workspace a project.

**Independent Test**: link a skill the agent does not have and write an instruction with a verifiable effect; in a filed conversation, confirm the skill is listed in the agent's catalogue and loadable, and the instruction is followed.

**Acceptance Scenarios**:

1. **Given** a linked skill, **When** an agent answers in a filed conversation, **Then** the skill is in its catalogue and its body loads on demand.
2. **Given** a linked skill the person chatting cannot see, **When** an agent answers for them, **Then** that skill is not offered.
3. **Given** workspace instructions, **When** any agent answers in a filed conversation, **Then** the instructions apply to that turn and to no turn outside the workspace.

---

### User Story 3 - Search the workspace's conversations (Priority: P2)

Every conversation filed in a workspace is indexed once it has been idle for a while. An agent working in the workspace can search all of them to recall what was decided, found or produced earlier, and links back to the source conversation.

**Why this priority**: valuable on its own but depends on the knowledge base lifecycle of Story 1.

**Independent Test**: in one filed conversation, establish a fact; let it go idle; in another filed conversation, ask about that fact and confirm the agent finds it and links the first conversation.

**Acceptance Scenarios**:

1. **Given** a filed conversation idle for the configured delay (FR-016), **When** the indexing runs, **Then** its messages, answers, attachment content and a summary become searchable, and its tool results do not.
2. **Given** an indexed conversation that receives new messages, **When** it is idle again, **Then** its index is replaced, never duplicated.
3. **Given** a conversation taken out of the workspace, moved to another one, or deleted, **When** the change is saved, **Then** it is no longer returned by the first workspace's search.
4. **Given** a workspace shared with a person, **When** an agent searches for them, **Then** results may come from every filed conversation, including other members' (FR-020).

---

### User Story 4 - Project page and in-conversation knowledge drawer (Priority: P2)

A person opens a workspace and sees a project page: a composer to start a conversation, the list of its conversations, and a knowledge panel with instructions, sources, linked knowledge bases and skills. Inside any filed conversation, a drawer shows the same knowledge without leaving the chat.

**Why this priority**: the API-level stories are usable from the existing surfaces; this makes them discoverable.

**Independent Test**: manage every knowledge element of a workspace from the project page and from the drawer of a filed conversation, as a manager and as a read-only member.

**Acceptance Scenarios**:

1. **Given** a manager, **When** they open the project page or the drawer, **Then** they can add and remove sources, skills and linked knowledge bases and edit the instructions.
2. **Given** a read-only member, **When** they open either view, **Then** they see the same elements without any control that changes them.
3. **Given** sources and conversations, **When** the page loads, **Then** each source shows its processing state and the conversation index shows how many conversations are indexed and pending.

---

### User Story 5 - Deleting a workspace cleans up its knowledge (Priority: P1)

A manager deletes a workspace. The Sources and Conversations knowledge bases it created disappear with their content; nothing that belongs to someone else is touched.

**Why this priority**: required by the request, and a storage leak otherwise.

**Independent Test**: create a workspace with sources, a synchronised source, an indexed conversation, a linked library knowledge base and a linked skill; delete it; confirm the two workspace knowledge bases and all their content are gone and the linked ones are intact.

**Acceptance Scenarios**:

1. **Given** a deleted workspace, **When** the deletion completes, **Then** its two knowledge bases, their documents, stored files, embeddings, synchronised sources and pending processing are removed.
2. **Given** a library knowledge base or skill linked to the deleted workspace, **When** the deletion completes, **Then** it still exists unchanged.
3. **Given** a deletion refused (default workspace, work items held), **When** it is refused, **Then** nothing is removed.

---

### User Story 6 - The same knowledge on every surface the workspace reaches (Priority: P3)

A sub-agent consulted or delegated from a filed conversation, a filed channel conversation (Teams, Slack…), and an agent node of a flow whose workspace node names the workspace receive the same instructions, sources, skills and conversation search as a web chat turn.

**Why this priority**: extends reach once the web path is proven.

A person chatting with an agent in Teams or Slack asks it to file the conversation into a workspace, or to create one for it.

**Independent Test**: for each surface, trigger a turn and confirm the instructions, a source fact, a linked skill and a conversation fact are available, under the same access rules as the web turn.

**Acceptance Scenarios**:

1. **Given** a consult or delegation from a filed conversation, **When** the sub-agent runs, **Then** it receives the workspace knowledge for the person who started the turn.
2. **Given** a flow whose workspace node names a workspace, **When** its agent nodes run, **Then** they receive that workspace's knowledge.
3. **Given** a filed channel conversation, **When** the agent answers there, **Then** it receives the workspace knowledge for the identified person, and none for an unidentified one.
4. **Given** a Teams or Slack conversation, **When** the person asks the agent to file it into a workspace, or to create one and file it there, **Then** the agent does so after stating that members of a shared workspace will be able to search it.
5. **Given** a filed conversation with the General Assistant, **When** it answers, **Then** it receives the workspace knowledge like any other agent.

### Edge Cases

- A source whose format or size the knowledge base limits refuse is rejected at upload with the reason, and nothing is stored.
- A source fails processing: it stays listed as failed with a retry, and turns ignore it.
- A source is removed during a turn that already received it: the turn completes; the next turn no longer has it.
- A linked skill or library knowledge base is deleted or unshared: it stops reaching turns at once and disappears from the workspace for the people who can no longer see it.
- A person loses access to the workspace: their turns in conversations still filed there receive no workspace knowledge (fail closed), and the two knowledge bases leave their library.
- The workspace is archived: its knowledge keeps reaching turns in already filed conversations; no new conversation can be filed (existing rule).
- A person asks the agent to file the conversation into a workspace they cannot configure, or an archived one: the agent reports the refusal and nothing is filed.
- A channel participant the platform cannot identify asks to file the conversation: refused, since the conversation has no owner to file it for.
- A Slack or Teams group-thread conversation filed into a workspace gets no workspace knowledge (FR-027c).
- A conversation filed by the agent mid-turn receives the workspace knowledge from its next turn.
- The first message of a new Teams or Slack thread asks to file it: the agent says filing works from the next message, since the conversation is recorded after the first answer.
- A conversation larger than one index document is split; every part links back to the conversation.
- An empty conversation, or one that only holds the agent's greeting, is not indexed.
- A conversation is deleted or unfiled while its indexing is in progress: the result is discarded.
- The workspace is deleted while sources or conversations are being processed: pending work is cancelled and writes nothing.
- The current conversation is never returned by the conversation search it runs from.
- A flow or caller that replaces the agent's knowledge-base or skill list for a turn still receives the workspace knowledge on top of the replacement.

## Requirements *(mandatory)*

### Functional Requirements

**Workspace knowledge bases**

- **FR-001**: Each workspace MUST own at most one Sources knowledge base and one Conversations knowledge base, created on first use.
- **FR-002**: The two knowledge bases MUST be visible in the knowledge-base library to exactly the people who can see the workspace, and follow every change to its sharing at once.
- **FR-003**: The library MUST mark them "Managed by workspace *name*" and link to the workspace; their settings, sharing and deletion MUST be refused outside the workspace, to everyone.
- **FR-003a**: Their content (documents, synchronised sources) MUST be writable exactly by the people who manage the workspace, and by no agent's write grant.
- **FR-004**: The two knowledge bases MUST NOT be offered in any picker that binds a knowledge base to an agent, an assignment, a flow or a person's defaults.

**Sources**

- **FR-005**: A manager MUST be able to add uploaded files, pasted text, a web page URL, a synchronised source, and a file attached in a filed conversation as sources.
- **FR-006**: Sources MUST use the formats, size limits, URL safety rules and processing of existing knowledge bases.
- **FR-007**: A manager MUST be able to remove a source, which removes its content from the Sources knowledge base.
- **FR-008**: When the processed sources together fit a context budget, a turn MUST receive all of them in full; otherwise it MUST receive a search over them, never a partial set in full.
- **FR-009**: The context budget MUST default to a share of the turn model's context window and be configurable by an administrator.
- **FR-010**: Answers that use a searched source MUST cite it with a link the reader can open.

**Linked knowledge bases, skills and instructions**

- **FR-011**: Knowledge bases already linked to a workspace MUST reach every turn in a filed conversation, read-only, limited to those the person the turn acts for can see.
- **FR-012**: A manager MUST be able to link and unlink library skills the manager can see.
- **FR-013**: Linked skills MUST be added to the turn's skill catalogue, loaded on demand, limited to those the person the turn acts for can see, on top of the agent's own skills.
- **FR-014**: A manager MUST be able to write workspace instructions, bounded in length, that apply to every turn in a filed conversation.

**Conversation search**

- **FR-015**: Every conversation filed in a workspace whose thread is its owner's own (web, direct message, platform chat) MUST be indexed in its Conversations knowledge base without any action from its author; a group channel thread, which holds other people's messages, MUST NOT be.
- **FR-016**: A conversation MUST be (re)indexed once it has been idle for a delay that defaults to 30 minutes and is configurable by an administrator.
- **FR-017**: The index of a conversation MUST hold its messages, the agent's answers, attachment content and a generated summary, and replace any earlier index of it.
- **FR-017a**: Tool results MUST NOT be indexed, nor fed to the summary, because they may carry data from integrations other members cannot reach.
- **FR-018**: A conversation MUST leave a workspace's index as soon as it is unfiled, moved to another workspace, archived (the product's delete) or deleted, or its owner can no longer see the workspace; it MUST NOT be indexed again until the owner sees it again.
- **FR-019**: Agents in a filed conversation MUST have a tool to search the workspace's conversations that returns passages with a link to their conversation and excludes the current one.
- **FR-020**: Conversation search MUST return results from every filed conversation to anyone who can see the workspace.
- **FR-021**: Content the platform stores as secret-redacted MUST stay redacted in the index.
- **FR-022**: Filing a conversation into a workspace shared with others MUST tell the person, before the conversation is filed, that its content becomes searchable by the workspace's members, and file it only once the person approved it themselves - never on an agent's word. Where the person cannot be asked, the agent MUST NOT file it there.

**Access and permissions**

- **FR-023**: Adding, removing or editing sources, linked knowledge bases, skills and instructions MUST require the workspace's config tier (owner, administrator, `config` or `config_with_chats` grant).
- **FR-024**: Reading the workspace's knowledge MUST require seeing the workspace, checked against the stored workspace on every request and every turn.
- **FR-025**: The same access rules MUST apply on the web chat, sub-agents, channels, flows and the existing agent-builder workspace tools, for access by id and by name.
- **FR-026**: A turn MUST receive the workspace knowledge for the person it acts for; an unidentified channel participant MUST receive none.

**Surfaces**

- **FR-027**: Workspace knowledge MUST reach any agent answering in a filed web conversation (the General Assistant included), sub-agents consulted or delegated from such a turn, filed channel conversations, and agent nodes of a flow whose workspace node names the workspace.
- **FR-027a**: An identified person's direct-message channel conversation MUST be fileable from their web conversation history, like any of their conversations.
- **FR-027b**: In any conversation, web or channel, the person MUST be able to ask the agent to create a workspace or to file the current conversation into one, under FR-022 and the existing filing rules (config tier on the target, not archived).
- **FR-027c**: A group channel thread MUST NOT receive workspace knowledge, even when filed, because its answers reach people the workspace is not shared with.
- **FR-028**: The XTM One MCP server MUST NOT serve workspace knowledge in this feature.
- **FR-029**: A workspace MUST have a project page with a composer, its conversations and a knowledge panel (instructions, sources, linked knowledge bases, skills, conversation index status).
- **FR-030**: A filed conversation MUST offer a knowledge drawer showing the same panel, with the same permissions.

**Deletion**

- **FR-031**: Deleting a workspace MUST delete its two knowledge bases with their documents, stored files, embeddings, synchronised sources and pending processing, in the same operation as the workspace.
- **FR-032**: Deleting a workspace MUST leave linked library knowledge bases and skills unchanged.
- **FR-033**: A refused deletion MUST change nothing.

### Key Entities

- **Workspace**: gains instructions; owns at most one Sources and one Conversations knowledge base.
- **Workspace Sources knowledge base**: a knowledge base owned by one workspace, with its visibility, holding the workspace's sources.
- **Workspace Conversations knowledge base**: same ownership; one indexed document set per filed conversation. Internal: no knowledge-base list, route or tool shows or changes it; only the workspace's conversation search reads it.
- **Workspace skill link**: a library skill linked to a workspace; deleting either end removes the link only.
- **Workspace knowledge base link**: the existing link between a workspace and a library knowledge base.
- **Conversation index state**: per filed conversation, when it was last indexed, for which content version, and in which workspace.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A manager adds a document to a workspace and gets an answer grounded on it in a filed conversation in under 2 minutes for a 20-page document.
- **SC-002**: 100% of turns in filed conversations receive the workspace instructions, linked skills and sources, on every surface of FR-027.
- **SC-003**: 0 turns outside a workspace, and 0 turns for a person who cannot see the workspace, receive its knowledge.
- **SC-004**: An idle filed conversation is searchable within the idle delay plus 5 minutes.
- **SC-005**: A conversation unfiled or deleted is absent from search results on the next search.
- **SC-006**: After a workspace deletion, 0 documents, files, embeddings or synchronised sources of its two knowledge bases remain.
- **SC-007**: A person reaches any knowledge element of a workspace from a filed conversation in at most 2 clicks.

## Assumptions

- A source's processing, formats and limits are those of today's knowledge bases; no workspace-specific quota.
- The conversation summary is generated by the platform's default model and is background platform work, like conversation compaction.
- A synchronised source acts with the integration the manager who configured it chose, as today.
- Direct-message channel conversations already appear in the web history and can be filed from it; FR-027b reuses the existing workspace tools' rules rather than adding new ones (research R12).
- An archived workspace keeps serving its knowledge to conversations already filed, consistent with the existing archive rule.
- Existing workspace knowledge-base bindings keep their current management API and UI; they are surfaced in the knowledge panel.
- Delivery is a stack of three pull requests: (1) sources, linked knowledge bases, skills, instructions, agent wiring and deletion; (2) conversation indexing and search; (3) project page and drawer. A GitHub issue must be created and referenced by each.
