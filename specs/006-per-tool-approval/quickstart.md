# Quickstart: Validating Per-Tool Approval

Runnable scenarios proving the feature end-to-end. Each maps to spec Success Criteria. Details live in [data-model.md](./data-model.md) and [contracts/](./contracts/) — not repeated here.

## Prerequisites

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build   # full stack (platform, worker, frontend), migrated on startup
```

Stop with `docker compose -f docker-compose.yml -f docker-compose.dev.yml stop`.

---

## Scenario 1 — A gated tool pauses, and the LLM cannot skip it (SC-001)

1. Create an agent (HITL now defaults on — verify no toggle was needed, **SC-007**).
2. Give it a tool that is NOT on the whitelist, and an assignment that will call it.
3. Trigger the assignment **manually** from the UI (this makes it interactive, per [contracts/events.md §6](./contracts/events.md)).

**Expect**: run enters `awaiting_human` *before* the tool executes; no side effect occurred; the run card shows tool name, description, and every argument as a form — not free text.

**Negative control**: run the same flow with a tool that IS on the shipped default whitelist (e.g. a `search_*`/`get_*` built-in). It must complete with no prompt (**SC-008**).

```bash
cd backend && pytest tests/test_tool_approval_gate.py -v
```

---

## Scenario 2 — Approve or reject (SC-002, SC-004)

Against a paused run from Scenario 1:

```bash
RUN=<run_id>
curl -s localhost:8100/api/v1/assignments/runs/$RUN/pending-approvals -H "Authorization: Bearer $TOKEN"

# approve as proposed
curl -X POST localhost:8100/api/v1/assignments/runs/$RUN/respond \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"decisions":[{"tool_call_id":"call_abc","decision":"approve"}]}'
```

Also verify:
- **Reject** — the agent receives a structured rejection and its next step reflects it; the run neither crashes nor stalls (**SC-004**).
- **Partial decisions** — omitting one pending `tool_call_id` returns **422**. This guards the orphaned-`tool_use` hazard; see [research.md D2](./research.md).

---

## Scenario 3 — Multi-call batch (the regression that matters most)

Prompt the agent so it emits **several tool calls in one turn**, at least one gated and one not.

**Expect**: *nothing* executes — including the ungated calls — and all proposals appear together. Then resume and confirm the conversation replays cleanly with no orphaned `tool_use` blocks and no LLM API error.

This is the highest-value test in the suite: raising mid-batch is what the current code does, and it corrupts the conversation once arbitrary tools can gate.

```bash
pytest tests/test_tool_approval_batch.py -v
```

---

## Scenario 4 — "Approve always" is a personal preference (SC-006)

1. As user A, resolve a proposal with `approve_always`.
2. Re-run the assignment as A → the tool no longer prompts.
3. Check `GET /api/v1/tool-approvals` → the preference is listed.
4. **As user B, run the same assignment on the same tool → B is still prompted** (FR-015a).
5. As an admin, confirm no cross-user listing exists; impersonate A and confirm the same endpoint now returns A's rows (FR-017a).
6. `DELETE /api/v1/tool-approvals/{id}` → the tool prompts A again.

Confirm audit records for grant and revoke, and that a revocation performed while impersonating carries **both** identities (FR-017b).

---

## Scenario 5 — Material tool change invalidates consent (FR-019a)

1. Grant `approve_always` for a custom tool.
2. Confirm it no longer prompts.
3. Edit only the tool's **description** → still no prompt (cosmetic).
4. Edit its **`input_schema`** → it prompts again. **Decline the prompt**, then check: the preference shows `revoked_reason = "tool_changed"` and an invalidation audit record exists (FR-019b).

    Declining matters. Re-granting on that same prompt supersedes the staleness *within the turn* — the row goes straight from the old fingerprint to the new one and is never marked revoked, so neither the reason nor the audit record appears. That is deliberate (`agent_chat.py`, `_chat_stale_approvals.discard`): draining afterwards would otherwise revoke the approval the reviewer had just re-given. The reject path is the only one where the invalidation is written, and therefore the only one that tests it.

---

## Scenario 5b — Whitelist administration and layering (SC-003, SC-012)

```bash
curl -s localhost:8100/api/v1/hitl-whitelist -H "Authorization: Bearer $ADMIN_TOKEN"
```

1. Confirm shipped-default entries are labelled `shipped_default` and admin ones `admin_added` (FR-001b).
2. **Add** a gated tool via `POST /hitl-whitelist` → it stops pausing. Confirm the UI warned before saving, and more prominently for a destructive tool (FR-005, **SC-003**). Confirm a `whitelist_tool_added` audit record exists.
3. **Remove** a tool that is in the shipped default → it starts pausing, and appears under `removed_from_default`.
4. Confirm any tool can be added, including a destructive one — the API must not refuse (FR-003).
5. **Simulate an upgrade**: edit `SHIPPED_DEFAULT_WHITELIST` to (a) re-add the tool removed in step 3 and (b) add a brand-new entry. Restart, then confirm:
   - the removal from step 3 still holds (FR-001e) — the upgrade did **not** silently re-exempt it;
   - the admin addition from step 2 still holds;
   - the brand-new default entry is active with **no** administrator action (FR-001f).

This is the highest-value whitelist test: step 5 is the only place the two-layer storage is actually proven, and getting it wrong silently un-gates a tool someone deliberately gated.

```bash
pytest tests/test_hitl_whitelist.py -v
```

---

## Scenario 5c — A connected platform's read tools (FR-001g/h)

Needs a real registered platform — OpenCTI or OpenAEV pointed at this XTM One, with its
integration connected. The derived layer is scoped to registrations, so it is invisible
without one.

```bash
curl -s localhost:8100/api/v1/hitl-whitelist -H "Authorization: Bearer $ADMIN_TOKEN" \
  | jq '.platform_reads, [.entries[] | select(.source=="platform_read")] | length'
```

1. Confirm the platform's read tools are listed with `source: "platform_read"` and the right
   `provider`, and that `platform_reads.<provider>.exempt` matches. A deployment with only
   OpenCTI connected must **not** list OpenAEV or OpenGRC names.
2. Set that platform's policy to **require** (Settings → Parameters → *Approval for connected
   platforms*). Then, in chat, ask the agent to **look something up** — `search_opencti`,
   `list_opencti_entities`. It must complete with **no prompt**. This is the point of the whole
   layer: `require` means *ask before anything that changes this platform*, not *ask before
   anything from it*.
3. Ask it to do something that **changes** the platform. It must pause. In particular ask for
   something that goes through a consolidated tool — `manage_opencti_pir` with `action: "list"`
   is the sharp case: it pauses even though the call only lists, because one tool call can also
   create and delete.
4. Ask it to read a file's contents (`download_opencti_file`) — the one read held back by
   hand rather than by classification (`HAND_GATED`). It must pause. Identity and RBAC
   reads are deliberately NOT held back, so asking who the users are will *not* pause.

   !!! warning "A refusal is not a pause"
       Asking OpenGRC to list users tends to make the model decline on its own — "for
       security reasons I can't enumerate platform users" — with nothing in the agent's
       deny list, allowed-tools or persona asking it to. **The tool is never called, so
       the step proves nothing**, and from the reviewer's chair a model refusal and a
       gate pause look identical. Confirm against the audit trail (`entity_type =
       'chat_tool_calls'`): no row, no tool call, inconclusive. `download_opencti_file`
       is the reliable one to test this step with, being a read nobody moralises about.

       Worth noticing rather than working around: FR-001 exists because the model must
       not be able to talk itself out of a pause, and this is the same gap from the other
       side — it talked itself out of the attempt. No gate can help, and the audit trail
       is the only evidence that distinguishes the two.
5. **Bulk re-gate**: `POST /hitl-whitelist/platform-reads/<provider>/require-approval`. Confirm
   the response counts what moved, the reads now pause, and the screen shows them under that
   platform's group with the button offering to restore. Call it twice — the second call must
   report `changed: 0` rather than re-asserting the set.
6. **The step that matters**: wait for the platform to re-register (its heartbeat, ~5 min) or
   restart it. The re-gating must still hold. Then simulate an upgrade the way Scenario 5b
   does and confirm it still holds. A heartbeat or a release silently restoring dozens of tools to
   unattended execution is the failure mode this layer is most likely to have.
7. `POST /hitl-whitelist/platform-reads/<provider>/restore-default` → the reads are exempt
   again, and the overrides are **cleared** rather than pinned, so a tool reclassified as a
   write in a later release starts gating again.
8. Confirm audit records exist for both bulk actions, naming the provider and the tools.
9. **The degraded state**, which is where a user-visible half went missing once already.
   Make the registrations unreadable by renaming the table, which fails the read
   deterministically and is reversible in one statement:

   ```sql
   ALTER TABLE platform_registrations RENAME TO platform_registrations_offline;
   -- restore: ALTER TABLE platform_registrations_offline RENAME TO platform_registrations;
   ```

   Not `REVOKE SELECT`: the application role owns this table in a default deployment, and
   revoking a privilege from its owner is not a reliable way to deny it.

   Then confirm all four of these, because the halves fail differently on purpose:
   - **The gate errs toward asking.** Ask the agent to look something up; the read now
     pauses. Unknown means exempt nothing, never exempt everything.
   - **The writes refuse.** `DELETE /hitl-whitelist/search_opencti` returns **503** and
     changes nothing. It must not answer 204 — that combination once told an operator a
     tool had been re-gated while it kept running unattended.
   - **The screen says so.** `GET /hitl-whitelist` still lists the curated half with
     `platform_reads_available: false`, and the whitelist section shows the notice rather
     than simply omitting the platform groups. An absent group reads as "this deployment
     has none", which an operator takes as "they were re-gated" — the opposite of the
     truth. The add field and the bulk buttons are disabled while it lasts.
   - **It recovers on its own.** Rename the table back; the reads are exempt again with
     no administrator action and no leftover override rows.

```bash
pytest tests/test_hitl_whitelist_platform.py tests/test_hitl_whitelist_api.py -v
DATABASE_URL=... pytest tests/test_hitl_whitelist_layering.py -v -m integration
python -m scripts.dump_platform_read_whitelist --check   # snapshot is current
```

---

## Scenario 6 — Exceptions reach scheduled runs; everything else pauses loudly (SC-010, SC-011)

1. As user A, grant `approve_always` for a state-changing tool — confirm the UI discloses that this also covers scheduled runs **before** you confirm (**SC-010**, FR-015c).
2. Configure the assignment on a **schedule** (no `triggered_by_user_id`) so it runs as A.
3. Let it fire.

**Expect**: the covered tool runs without pausing (FR-015b). A *different* gated tool with no exception still pauses, and A receives a notification identifying the assignment and the pending tool (FR-015f, FR-015d).

**Then repeat with a company-managed run** — this is the case research flagged: `_persist_notification_activity` skips durable records for company-managed runs (`notify.py:120-125`). A durable notification must still be produced, or an autonomous company-managed pause is invisible.

Confirm the run stays paused indefinitely with no timeout or auto-resolution (FR-015e).

```bash
pytest tests/test_tool_approval_autonomous.py -m integration -v
```

---

## Scenario 7 — Upgrade behaviour (SC-007, SC-009)

On a snapshot of a pre-upgrade database:
```bash
cd backend && alembic upgrade head
psql "$DATABASE_URL" -c "SELECT approve_tool_calls_default FROM platform_settings;"            # expect f
psql "$DATABASE_URL" -c "SELECT count(*) FROM agents WHERE approve_tool_calls IS NOT NULL;"     # expect 0
```

Run a scheduled assignment that calls a non-whitelisted tool: it completes with no pause. Create an agent afterwards: it does not gate either. Set one agent's **Tool approvals** to **On**: only its next run pauses. On a fresh install the default reads `t` and a new agent gates with no configuration.

```bash
pytest tests/test_approval_upgrade_migration.py -m integration -v
pytest tests/test_approve_tool_calls_switch.py -v
```

---

## Scenario 8 — Backward compatibility (FR-011)

The `request_human_input` builtin must behave exactly as before: a run paused by it resumes from free text via `{"response": "..."}`.

Include a run **paused before the upgrade** (no `pending_approvals`) and confirm it still resumes — this exercises the fall-through branch described in [research.md D5](./research.md).

```bash
pytest tests/test_hitl_response_persistence.py -v
```

---

## Scenario 9 — Tool approvals on a built-in agent (SC-014, SC-015)

Built-in agents are read-only in XTM One mode; `approve_tool_calls` is the exception beside `enabled` (FR-012b). Run this as an **administrator**, on a built-in agent — one seeded from `BUILTIN_AGENTS`, not one you created.

1. Open the agent's **Configuration** tab. Everything is read-only except the **Tool approvals** card.
2. Set it to **Off**. The confirmation must state that this reaches the agent's **chats** as well as its runs.
3. Confirm, then check the audit log names the value set:

```bash
psql "$DATABASE_URL" -c "SELECT details->>'approve_tool_calls', user_id, created_at FROM audit_logs WHERE entity_type = 'agent' ORDER BY created_at DESC LIMIT 1;"
```

**Still refused**:

```bash
# human_in_the_loop and its prompt stay product-owned on a built-in: expect 403.
curl -sS -X PUT "$BASE/api/v1/agents/<builtin-agent-id>" -H "$AUTH" \
  -d '{"human_in_the_loop": true}' -o /dev/null -w '%{http_code}\n'
# The agent's assignments stay read-only, and a non-admin may not set it: expect 403.
```

**It survives a restart** — the boot-time re-seed must not revert it:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml restart platform   # re-runs the built-in agent seed
psql "$DATABASE_URL" -c "SELECT slug, approve_tool_calls FROM agents WHERE is_builtin = true;"
```

```bash
pytest tests/test_builtin_hitl_admin_writable.py tests/test_builtin_hitl_endpoint_wiring.py \
       tests/test_builtin_seed_preserves_hitl.py -v
```

---

## Scenario 10 — The General Assistant asks (FR-002b1, FR-002c4, FR-008a, FR-012a1)

!!! warning "Read this before step 1 — the General Assistant is not reachable by default"
    In `xtm_one` mode, startup **links** the General Assistant to the CTEM Assistant on every
    boot (`backend/app/main.py:1352-1392`), and the picker **hides** the standalone General
    Assistant entry whenever a linked agent is set
    (`frontend/src/pages/chat/ConversationSidebar.tsx:141`). The same startup pass also sets
    `users.default_agent_id` to the CTEM Assistant for every user whose default is `NULL` —
    which is exactly what "General Assistant" means in Profile — so a profile set to the GA is
    reassigned on **every** restart, not once.

    Skip the unlink and you will exercise `run_agent_chat` on an ordinary agent, see approvals
    work correctly, and conclude this scenario passes. **It proves nothing about
    `run_general_chat`.** Same shape as the refusal trap in Scenario 5c: the surface looks right
    while the code under test was never reached.

**Preamble**: Settings → General Assistant → linked agent → **none**. Then pick *General
Assistant* in the conversation's agent picker, or set Profile → Default chat agent to it. Re-do
both after any stack restart, or temporarily rename the `ctem-assistant` slug so the startup
block finds nothing.

1. Ask it to do something a write-classified builtin handles — "create an agent that triages
   phishing reports" (`create_agent`), or "make me a template for X" (`create_template`). None
   of these ships on the whitelist, and no platform registration is needed, unlike Scenario 5c.
   Confirm the builtin's tool group is enabled for the GA in Settings first; each is opt-in.
2. **Expect the prompt inside the conversation, and the same turn to continue** with the tool
   result after you approve (FR-002c). This surface no longer answers with prose explaining
   that it could not collect an approval — that message is now correct **only** on the public
   embed.
3. Reject one, and approve-always another. The "always" must persist: ask again and it must not
   prompt. Check Profile → tool approvals lists it (FR-017).
4. Take longer than `chat_loop_timeout` to answer. The turn must still be alive when you decide
   — the deadline is rescheduled around the wait, not merely generous.
5. The General Assistant has **no switch of its own** (FR-012a1): it follows **Agents ask by
   default** under Settings → Parameters → Tool Approvals. Turn that off and it stops asking.
6. Ask the same thing over Slack, Teams or email. Until Phase 17's T156 lands, expect the
   explanatory message — never "I encountered an error while processing your message".
7. **Second-reviewer refusal** (FR-008a): with a proposal pending, submit a decision as a
   different user. It must be refused, recorded, and the proposal must stay pending.
8. **Supersession** (FR-002c4): with a proposal pending, send a new message instead of deciding.
   The proposal is retired, and a decision submitted for it afterwards is refused rather than
   executed.

**Check the audit trail, not the reply.** A model refusing on its own and a model answering from
the knowledge base both produce no gated call and no audit row, and from the reviewer's chair
look identical to a pause. `entity_type = 'chat_tool_calls'` is the only arbiter.

---

## Full gate before pushing

```bash
./scripts/ci-local.sh          # same checks CI runs
cd backend && pytest -m "not integration" && pytest -m integration
cd frontend && yarn tsc --noEmit && yarn lint
```

Migrations additionally face the CI job's idempotent-re-run and downgrade-round-trip checks (Constitution Principle V).

---

## Coverage map

| Scenario | Success Criteria |
|---|---|
| 1 | SC-001, SC-007, SC-008 |
| 2 | SC-002, SC-004 |
| 3 | — (regression guard for research D2) |
| 4 | SC-006 |
| 5, 5b | FR-019a/b · SC-003, SC-012 |
| 5c | FR-001g/h — derived platform reads, `manage_*` still gated, bulk re-gate survives re-registration; step 9 covers the unreadable-registrations state (gate asks, writes 503, screen says unknown) |
| 6 | SC-010, SC-011 |
| 7 | SC-009 |
| 8 | — (FR-011) |
| 9 | SC-014, SC-015 · FR-012b/c/d/e |
| 10 | SC-016, SC-017 · FR-002b1, FR-002c4, FR-008a, FR-012a1 — the General Assistant on the browser socket; steps 6-8 stay red until Phase 17 Phase 2 |
| Audit assertions in 2, 4, 5 | SC-005 |
| Whitelist add warning (frontend) | SC-003 |

---

## Scenario 10 — Answer a pause without leaving the page (SC-018, SC-019, SC-022)

**Precondition to check first** ([research.md §D12](./research.md)): confirm a per-tool gate pause
appears in the header at all. `Header.tsx` filters on `human_input_question` being truthy; if a
gate pause leaves it empty the item never renders, and everything below tests nothing.

1. Trigger a scheduled assignment that calls a non-whitelisted tool. It pauses.
2. From an **unrelated** page (say Settings), open the notification bell. The pause is listed and
   marked as needing action.
3. Expand it. The tool name, its assignment and run, and the **complete** proposed arguments are
   shown — compare byte-for-byte against `GET /runs/{id}/pending-approvals`.
4. Approve. The run resumes; the Settings page beneath has not navigated, reloaded, or lost form
   state.
5. Open the run afterwards. The decision, its outcome and its audit attribution read identically to
   one made on the run page.

**Expected**: identical audit content to Scenario 2's run-page decision. Any difference is FR-029
failing.

---

## Scenario 11 — A group across runs, with reading enforced (SC-021, FR-031a, FR-034)

1. Pause three runs of the same assignment on the same tool, and one run on a different tool.
2. Use the **by-tool** shortcut. Three are selected; the fourth is not.
3. Add the fourth by hand, then deselect one of the three. The shortcut is an aid, not a
   constraint (FR-031a).
4. With one member never expanded, try the group action. It is refused, and the count of unread
   members is stated — not merely a disabled button (FR-034).
5. Expand the remaining member, then approve. All selected runs resume; each has its own audit
   entry; there is no batch-level record (FR-032).
6. Confirm the confirmation named that the selection spanned more than one tool (FR-031b).

**Expected**: N decisions, N audit entries, zero standing exceptions granted.

---

## Scenario 12 — Partial failure is a success (FR-033, SC-023)

1. Select three pending approvals.
2. In a second tab, answer one of them on its run page.
3. Return and submit the group.

**Expected**: `200`. Two `applied`, one `refused` with `already_answered` naming the decision that
won. The two that applied are **not** rolled back, and no second decision exists for the third
(SC-023).

---

## Scenario 13 — "Approve always", single and over a group (FR-037, FR-032a)

1. Expand a single proposal and choose "approve always". Before confirming, the disclosure states
   that the exception also un-gates unattended execution of that tool on every run carrying your
   identity (FR-037a, FR-015c).
2. Confirm. Check the granting user's own preferences: the exception is there, revocable, and
   indistinguishable from one granted on the run page (FR-037b).
3. Select two rows and choose **Approve always**. The confirm names **every** distinct tool it would
   un-gate, says the effect reaches unattended runs, and says it is revocable (FR-032b).
4. Confirm.

**Expected**: one standing exception per distinct tool, each identical to a run-page grant, and one
decision recorded per member — never a single blanket grant (FR-032).

---

## Scenario 14 — The count is not the page (FR-035, SC-024)

1. Create 25 completed-run notifications and 3 pending approvals.
2. Without opening the list, read the indicator.

**Expected**: 3 approvals awaiting a decision, distinguishable from unread notices. A count derived
from the 20-run page would be wrong here, which is the point ([research.md §D13](./research.md)).

