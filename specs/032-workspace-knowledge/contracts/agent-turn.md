# Contract: What a Turn Receives, and the Agent Tools

## Workspace knowledge in a turn (PR1)

`resolve_turn_workspace_knowledge` (R5) yields, for a turn acting for an identified person who can see the workspace:

| Element | How the agent receives it | Spec |
|---|---|---|
| Instructions | System prompt section `workspace_instructions` | FR-014 |
| Sources, full mode | One quarantined block listing each source's title and text | FR-008 |
| Sources, search mode | Sources KB id added read-only to the turn's KB ids, served by `search_knowledge` and the document-read tools | FR-008, FR-010 |
| Bound KBs | Visible ids added read-only to the turn's KB ids | FR-011 |
| Skills | Added to the catalogue and loaded on demand, under the turn user's visibility | FR-013 |
| Conversation search | `search_workspace_conversations` (PR2) | FR-019 |

The resolver returns `None` when the turn's workspace is not visible to the person, or when the participant is anonymous. In that case the agent receives none of these elements (FR-024, FR-026).

| Surface | Workspace id source |
|---|---|
| Web chat, transfer | The conversation's `workspace_id` |
| Channel | The conversation's `workspace_id` (none on a thread's first message) |
| General Assistant | The conversation's `workspace_id` |
| Consult | `session_workspace_id` from the parent turn |
| Delegate | The workspace of `SubAgentTask.conversation_id` |
| Flow | A `capability.workspace` node wired to the agent node |
| XTM One MCP | Never (FR-028) |

A session replacement of the agent's KB or skill lists, such as a flow's capability nodes, replaces the agent's own entries only. The workspace entries are added on top.

## `search_workspace_conversations` (PR2)

```json
{
  "name": "search_workspace_conversations",
  "parameters": {
    "query": {"type": "string", "minLength": 1, "maxLength": 1000},
    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5}
  }
}
```

The tool returns passages, each headed by the conversation's escaped title and a link built with the base path. The current conversation is excluded, and so is every conversation that is no longer eligible (R10). A blank query is refused with a message, never raised.

## `workspace_filing` suite (PR2)

It is offered on a turn with an identified person and a conversation, and skipped when `agent_builder` already provides the same tools.

| Tool | Parameters | Result |
|---|---|---|
| `list_workspaces` | — | The workspaces the person can see, with `can_manage` and `shared` |
| `create_workspace` | `name (1-200)`, `description?` | The created workspace, owned by the person |
| `file_this_conversation` | `workspace_id` | Filed, or a refusal |

`file_this_conversation` behaves as follows:
1. It refuses without filing when:
   - the target is archived;
   - the target is below the person's config tier;
   - the person does not own the conversation;
   - there is no conversation yet (first channel message).
2. When the target is not one of the person's own private workspaces, the call pauses on the approval card (the turn executor's consent screen, whatever the agent's approval settings) and runs only once the person approves it. `move_conversations_to_workspace` is screened the same way; where nothing screens (MCP, assignment run, sub-agent), a target that reaches others is refused.
3. Workspace knowledge applies from the next turn.

## `capability.workspace` flow node (PR1)

Config: `{workspace_id}`.
- The node is resolved when the run starts with `resolve_workspace_scope`, as the flow's acting user. A missing, invisible or archived workspace fails the run closed, with the reason logged.
- Wired agent nodes receive `session_workspace_id`.
