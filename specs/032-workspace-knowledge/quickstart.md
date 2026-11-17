# Quickstart: Validating Workspace Knowledge

## Prerequisites

- Local stack running (`./dev-podman.sh` or `make up`), migrations at head, a configured chat LLM and embedding model, and the SAQ worker running (document processing and conversation indexing).
- Two users: `owner` (non-admin) and `reader` (non-admin). One agent with no knowledge base and no skill of its own.

## PR1: sources, instructions, linked KBs, skills, cleanup

1. **Create and configure the workspace.** As `owner`, create workspace `W`, share it with `reader` as `read_only`, and set the instructions to "Always end your answer with the word ACK." Verify: `GET /workspaces/{W}` returns the instructions.
2. **Add a source.** Add a text source containing a fact found nowhere else, then poll `GET /workspaces/{W}/sources` until it is `ready`.
3. **Ask in a filed conversation.** Create a conversation filed in `W` with the agent and ask about the fact. Expected: the answer uses the fact and ends with ACK (US1-1, US2-3).
4. **Ask outside the workspace.** In an unfiled conversation, ask the same question. Expected: no fact, no ACK (US1-3).
5. **Lower the budget.** Set `sources_context_percent` to 5 and add a source large enough to exceed it. Ask again. Expected: the trace shows a `search_knowledge` call on the Sources KB, and the answer cites the passage (US1-2).
6. **Link a skill.** Link a library skill visible to `owner` only. As `owner`, the agent's catalogue lists it. As `reader`, in their own conversation filed in `W`, it does not (US2-2).
7. **Check the library.** As `reader`, the KB library lists "W — Sources" with the badge. Uploading to it from the KB page answers 409. It is absent from the agent KB picker (FR-002 to FR-004).
8. **Unshare.** Remove `reader` from `W`. Their next turn in a conversation filed in `W` receives no instructions or sources, and the KB leaves their library (edge case on lost access).
9. **Delete the workspace.** Delete `W`. Verify the cleanup with:

   ```sql
   SELECT count(*) FROM knowledge_bases WHERE owner_workspace_id = '<W>';
   ```

   It returns 0. The linked library KB and the linked skill still exist (US5).

## PR2: conversation search and filing

1. **Index an idle conversation.** Set `conversation_idle_minutes` to 5. In conversation A filed in `W`, establish a fact through a tool call and in plain text. Wait 5 minutes plus one tick. Verify: `GET /workspaces/{W}/conversation-index` counts A as indexed.
2. **Search from another conversation.** In conversation B filed in `W`, ask about the plain-text fact. Expected: `search_workspace_conversations` finds it, links A, and never returns the tool-result text (US3-1, FR-017a).
3. **Archive the source conversation.** Archive A and ask again. Expected: no result from A (FR-018).
4. **File from a channel.** In a Teams DM (second message onward), ask the agent to file the conversation into `W`. Expected: the agent states the shared-workspace notice, files after you confirm, and the next turn has the instructions (US6-4).

## Automated checks

```bash
cd backend && uv run pytest tests/test_workspace_knowledge*.py tests/test_internal_kb_visibility.py tests/test_visibility_helpers.py -n 4
```

```bash
cd backend && uv run pytest -m integration tests/test_workspaces_postgres.py tests/test_workspace_knowledge_postgres.py
```

```bash
./scripts/ci-local.sh --no-e2e
```
