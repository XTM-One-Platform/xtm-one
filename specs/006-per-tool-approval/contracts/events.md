# Contract: Real-Time Events & Internal Interfaces


> **Surface names** follow the `Approval Surface` entity in [spec.md](../spec.md):
> browser socket, REST, platform stream, chat channels, public embed. "Chat" alone
> spans the first four, and "chatbot" means the external `filigran-chatbot`
> package — nothing in this repository.

---

## 1. `action_proposal` SSE event (FR-007)

**Transport**: Redis pub/sub → `GET /api/v1/activity/stream` (`api/v1/activity.py:1184`).

The SSE endpoint is a **raw passthrough** — it emits no SSE `event:` names (`yield f"data: {message['data']}\n\n"`), so routing is entirely via the JSON `type` field. Clients switch on `data.type`, as `useActivityStream.ts:53-56` already does for `background_task_update`.

**Channel**: `activity:{run.user_id}`, or `activity:company` when `run.is_company_managed` — identical routing to `_publish_run_status` (`assignment_helpers.py:189-232`).

**Payload**
```json
{
  "type": "action_proposal",
  "run_id": "uuid",
  "assignment_name": "Nightly enrichment",
  "is_autonomous": true,
  "proposals": [
    {
      "tool_call_id": "call_abc123",
      "tool_name": "opencti_delete_entity",
      "tool_description": "Permanently delete an entity from the platform.",
      "arguments": {"entity_id": "…"},
      "input_schema": {"type": "object", "properties": {}},
      "source": "integration:opencti"
    }
  ]
}
```

Emitted at the moment the run pauses, in the same block that sets `status = "awaiting_human"` — so the existing `assignment_run` status event and this one arrive together. Consumers MUST tolerate either order.

`is_autonomous` lets the UI mark a proposal that nobody is actively waiting on, which is the case FR-015d notifies about.

---

## 2. `HumanInputRequestedError` (extended)

`backend/app/tools/base.py:346-361`. Existing fields `question`, `tool_call_id`, `conversation` are unchanged so the free-text path keeps working (FR-011).

```python
class HumanInputRequestedError(Exception):
    question: str
    tool_call_id: str
    conversation: list
    proposals: list[ActionProposal] | None = None   # NEW — None ⇒ free-text pause
```

`proposals is None` means a classic `request_human_input` pause; a populated list means an approval gate fired. Every consumer that today reads only `question` continues to work.

**Propagation is already correct** and must stay so — the error passes untouched through `CallableToolExecutor` (`base.py:880-881`), `CompositeToolExecutor` (`:1029`), and `integration_tools.py:1134`. Any new `except Exception` added along the tool path MUST re-raise it.

---

## 3. Gate decision function

`backend/app/tools/approval.py` — pure, synchronous, no I/O. Implements the decision table in [data-model.md §6](../data-model.md).

```python
def evaluate_gate(
    definition: ToolDefinition,
    *,
    hitl_enabled: bool,
    whitelist: frozenset[str],
    # NOTE: interactivity is NOT an input. A standing exception applies to
    # interactive and autonomous runs alike (FR-015b); interactivity only
    # decides whether a pause notifies the owner (FR-015d).
    active_exceptions: Mapping[str, str],   # tool_key -> schema_fingerprint
) -> GateDecision: ...
```

`GateDecision` carries `action: Literal["execute", "pause"]`, `log_outcome: str` (the FR-020 gate outcome), and `invalidated_preference: bool` (row 4 of the decision table).

**Contract**: the function is total — it MUST return a decision for every input combination and MUST NOT raise. Callers treat any exception during exception-lookup as `pause` (Principle I).

`hitl_enabled` is `resolve_approve_tool_calls(db, agent)` (`hitl_whitelist.py`): the agent's `approve_tool_calls`, else the install default (FR-012, FR-014); an unreadable default resolves to `true`. When it is false no gate is installed, and each call is recorded as `auto_run_hitl_off` (the agent's own switch) or `auto_run_install_default_off` (the install default, FR-014b).

### Wholesale suppression

The two switches of FR-023/FR-024 are resolved **before** this function, not inside it — they decide whether the gate
above is installed at all, and folding them in as a third input would make a pure decision function answerable for
reading settings.

On a hit the caller installs a **recording-only** gate rather than no gate: `action` is always `"execute"`, and
`log_outcome` is one of two values, kept distinct because they name different responsible parties.

| `log_outcome` | Set when | FR |
|---|---|---|
| `auto_run_approvals_disabled` | An administrator switched approvals off platform-wide | FR-023 |
| `auto_run_user_allows_all` | The run's own user set allow-all | FR-024 |

Both join the FR-020 enumeration, so a suppressed call is still distinguishable in the log from an ungated one, from one
covered by a standing exception, and from the `null` that means a gate failed to install.

---

## 4. HITL whitelist resolution

`backend/app/services/hitl_whitelist.py`

```python
async def resolve_whitelist(db) -> frozenset[str]:
    """Effective membership: (shipped default − removals) ∪ additions."""
```

**Contract**: resolved **once per run** and cached for its lifetime — membership is consulted on every tool call, so a per-call query is not acceptable. A resolution failure MUST propagate to the caller as an error, never as an empty set: an empty set is indistinguishable from "nothing is whitelisted" and would gate everything, but a silently-empty result from a *partial* read could just as easily under-gate. The caller treats any failure as "gate everything" (Principle I).

The shipped default lives in `backend/app/services/hitl_whitelist_default.py` as `SHIPPED_DEFAULT_WHITELIST: frozenset[str]` plus `SHIPPED_WHITELIST_VERSION: int`. It is **data, reviewed as data** — changing it is a content change, not a logic change, and no code should branch on the version.

---

## 5. Schema fingerprint

`backend/app/tools/approval.py`

```python
def fingerprint(definition: ToolDefinition) -> str:   # 64-char hex
```

Hashes **only** the canonically-serialised `input_schema`. It MUST NOT include `description`, `name`, `priority`, or whitelist membership — implementing FR-019a's distinction between cosmetic and material change. Serialisation must be key-order-stable so an unchanged schema always yields the same digest. Whitelist membership is excluded deliberately: whitelisting a tool makes an exception moot rather than invalid, since a whitelisted tool never gates for anyone.

---

## 6. Interactivity determination

`backend/app/services/assignment_helpers.py`

```python
def is_interactive_run(run: AssignmentRun) -> bool:
    """True iff a human initiated this run and may be watching."""
    return run.triggered_by_user_id is not None
```

Any run without that signal — schedule, webhook, integration event, or an unrecognised future trigger — is autonomous, so a pause notifies rather than staying silent. This function feeds notification routing only; it is deliberately **not** an input to `evaluate_gate`.

---

## 7. Notification for autonomous pauses (FR-015d)

Reuses `deliver_notification` (`notify.py:29-84`) with one required change.

**Current gap**: `_persist_notification_activity` returns early when `run.is_company_managed` or `run.user_id` is falsy (`notify.py:120-125`), leaving only an ephemeral `activity:company` broadcast. For an autonomous company-managed pause — exactly what FR-015d covers — nobody connected at that instant ever learns the run stalled.

**Contract**: when `is_interactive_run(run)` is false, a durable notification record MUST be created regardless of `is_company_managed`. The notification identifies the assignment, the tool awaiting approval, and how to act on it.

Two constraints on the record itself:

- **It must fit its column.** `Activity.title` is `String(500)` and both halves of the title are unbounded — a user-named assignment and a batch of any size. The write is best-effort inside a `try/except`, so an over-long title is not an error anyone sees: it is a pause that goes unnotified, which is the one outcome this record exists to prevent. Names are capped with "and N more" and the title trimmed; the full list stays in `extra_data`.
- **An owner-less run is still not covered.** `audit_logs.user_id` is `NOT NULL` and such a run has no identity to attribute an Activity to, so only the ephemeral broadcast reaches whoever is connected. Known trade rather than an oversight — recorded here so it is decided rather than discovered.
