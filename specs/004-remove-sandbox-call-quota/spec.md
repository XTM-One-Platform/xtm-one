# Feature Specification: Remove Platform-Wide Sandbox Call Quota

**Feature Branch**: `004-remove-sandbox-call-quota`

**Created**: 2026-08-03

**Status**: Draft

**Input**: User description: "Remove the platform-wide OpenSandbox call quota feature (spec 001-sandbox-call-quotas) entirely, since it is now redundant with spec 002-sandbox-quota-usage-tracking's per-user combined agentic+sandbox enforcement (FR-015-FR-019). This includes: check_sandbox_quota, record_sandbox_call, SandboxQuotaExceededError, sandbox_quota_message, and the sandbox_quota.py module's platform-wide-cap logic; the sandbox_quota_usage table and its migration (needs a real downgrade path); the SANDBOX_QUOTA_LIMITS and SANDBOX_QUOTA_FAIL_MODE configuration variables; all associated tests. The three OpenSandbox call sites currently call check_sandbox_quota() before spec 002's newer per-user combined check — that anchor point needs to be removed and the remaining code adjusted so spec 002's combined check becomes the sole quota gate at dispatch time. This also requires amending spec 002 (FR-009) and spec 003-sandbox-call-logging (FR-011), both of which currently require the removed mechanism to keep working unchanged."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Retire the redundant platform-wide quota mechanism (Priority: P1)

As a platform maintainer, I want the platform-wide OpenSandbox call quota (spec `001-sandbox-call-quotas`) removed, so that sandbox dispatch has a single, simple quota enforcement path instead of two overlapping mechanisms that both exist only to prevent runaway sandbox usage.

**Why this priority**: This is the entire point of the feature. Spec `002-sandbox-quota-usage-tracking`'s per-user combined agentic+sandbox check (FR-015–FR-019) already blocks excessive sandbox usage; keeping the old platform-wide cap alongside it is pure duplication with no remaining protective value, and duplication is itself a maintenance and comprehension cost.

**Independent Test**: Can be fully tested by confirming that after removal, no code path references the platform-wide sandbox call counter, no configuration variable controls it, and sandbox dispatch still succeeds/fails based solely on the per-user combined check.

**Acceptance Scenarios**:

1. **Given** the removal has shipped, **When** any of the three OpenSandbox-routed execution paths (code interpreter, custom JS tool, custom Python tool) dispatches a call, **Then** no platform-wide, all-users-combined quota check runs at any point in that dispatch.
2. **Given** a deployment that previously configured a platform-wide daily/weekly/monthly sandbox call limit, **When** that deployment upgrades to this change, **Then** the platform starts normally with no reference to that limit anywhere, and sandbox calls are no longer bounded by it.
3. **Given** the removal has shipped, **When** a developer searches the codebase for the removed functions/classes (the platform-wide check, its exception type, its message-formatting helper, and its call-recording function), **Then** no reachable code references them.

---

### User Story 2 - Sandbox dispatch keeps working, gated solely by the per-user check (Priority: P1)

As a user of the platform, I want my sandbox executions (code interpreter, custom tools) to keep working exactly as they do today — refused only when *my* combined agentic+sandbox usage exceeds *my* limit — without a second, invisible platform-wide gate that could refuse my call for reasons unrelated to my own usage.

**Why this priority**: Removing the old mechanism must not create a gap where calls are dispatched completely unchecked, nor break the per-user enforcement that is now the sole safeguard. This is equally foundational to User Story 1 — a removal that breaks the remaining enforcement, or removes enforcement entirely, is not an acceptable outcome.

**Independent Test**: Can be fully tested by driving a user's combined agentic+sandbox usage to their configured limit and confirming sandbox dispatch is still refused at exactly that point (unchanged from spec 002's existing behavior), with no other, unrelated refusal reason ever appearing.

**Acceptance Scenarios**:

1. **Given** a user is under their combined agentic+sandbox limit, **When** they trigger a sandbox execution, **Then** it proceeds normally, identical to today's behavior.
2. **Given** a user is at or over their combined agentic+sandbox limit, **When** they trigger a sandbox execution, **Then** it is refused by the per-user combined check, with the same refusal behavior spec 002 already defines (FR-018/FR-019) — unchanged by this removal.
3. **Given** the removal has shipped, **When** any of the three call sites is inspected, **Then** the per-user combined check is the only quota-related check remaining before the sandbox is actually created.

---

### User Story 3 - Keep the spec suite internally consistent (Priority: P2)

As a maintainer reading specs `002-sandbox-quota-usage-tracking` and `003-sandbox-call-logging`, I want them to accurately describe the system after this removal, so that neither spec asserts a requirement about a mechanism that no longer exists.

**Why this priority**: Spec 002's FR-009 currently requires that "the existing platform-wide sandbox call limits... continue to operate independently," and spec 003's FR-011 requires that "the existing quota-check or quota-recording behavior (`sandbox_quota_usage` ledger)" stay unchanged. Once this removal ships, both statements describe something that no longer exists — leaving them as-is would make the spec suite self-contradictory and mislead a future reader or implementer. This is lower priority than the removal itself (Stories 1–2) because it's a documentation-consistency concern, not a runtime behavior change.

**Independent Test**: Can be fully tested by reading spec 002 and spec 003 after this change ships and confirming neither contains a requirement, success criterion, or acceptance scenario that depends on the removed platform-wide mechanism still existing.

**Acceptance Scenarios**:

1. **Given** this removal has shipped, **When** spec 002 is reviewed, **Then** FR-009 (and any success criteria/scenarios referencing the platform-wide cap "continuing to operate unchanged") is updated to reflect that the platform-wide cap no longer exists.
2. **Given** this removal has shipped, **When** spec 003 is reviewed, **Then** FR-011 is updated to reflect that the `sandbox_quota_usage` ledger it referenced no longer exists.
3. **Given** either spec is updated, **When** the update is made, **Then** it does not alter any requirement about spec 002's own per-user combined check or spec 003's per-call logging — only the now-moot references to the removed mechanism change.

---

### User Story 4 - Remove the never-merged migration cleanly, without a downgrade path (Priority: P2)

As a maintainer of this feature branch, I want the migration that created the platform-wide quota's database table removed by editing/deleting it directly at its source, rather than by adding a new, separate migration on top of it that reverses it, so that the branch's history reads as if the table was never introduced — since that migration has never been merged into the main branch and therefore carries no released-deployment compatibility obligation.

**Why this priority**: This project's migration reversibility rule (constitution Principle V / backend `AGENTS.md` Rule 6) exists to protect *already-released* schema history for real deployments — it is not a reason to pile a second, purely-corrective migration on top of a first one that has never shipped outside this branch. Editing the original migration in place is simpler and leaves a cleaner history. This is secondary to Stories 1–2 because it concerns *how* the removal is expressed in migration history, not whether the removal itself is correct.

**Independent Test**: Can be fully tested by confirming the migration chain no longer contains any revision that creates the removed table, and that a database which already applied the now-removed migration (e.g. this branch's own local development database) can be reconciled to the new chain with a one-time manual fix, not a shipped migration step.

**Acceptance Scenarios**:

1. **Given** the migration that created the platform-wide quota's table exists only on this unmerged branch, **When** this change is made, **Then** that migration is edited or deleted directly rather than counter-acted by a new migration.
2. **Given** a fresh install after this change, **When** the migration chain runs, **Then** the removed table is never created — there is no migration step that creates it and no separate step that drops it.
3. **Given** a database that already applied the now-removed migration (this branch's own development/CI databases), **When** it is brought in line with the edited chain, **Then** this is treated as a one-time, local reconciliation (drop the table, re-point the recorded migration state to the new chain tip) — not a mechanism that ships to any real deployment.

### Edge Cases

- What happens to sandbox-call usage data already recorded in the removed table on an existing deployment? It is dropped along with the table — this was transient operational counter data (call counts for expired quota periods), not business-critical or user-facing data, so no data-preservation or export step is required (see Assumptions).
- What happens on a deployment that still has the removed configuration variables (the platform-wide daily/weekly/monthly limits and their fail-mode setting) set in its environment after upgrading? Startup MUST NOT fail because of them — they are simply no longer read by anything, consistent with how this codebase's configuration already ignores unrecognized environment variables rather than erroring on them.
- What happens to a sandbox call that is mid-flight (already past the per-user check, sandbox already created) at the moment this change is deployed? Out of scope — this is a code/schema deployment concern common to any release, not specific to this removal, and is already handled by the platform's existing rolling-deploy discipline.
- What happens if a future feature wants a platform-wide cap again? Out of scope for this removal — re-introducing platform-wide enforcement, if ever needed, is a new feature decision, not something this removal needs to keep an extension point for.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST NOT perform any platform-wide (all-users-combined) sandbox call quota check at any point during OpenSandbox-routed dispatch (code interpreter, custom JS tool, custom Python tool).
- **FR-002**: The per-user combined agentic+sandbox quota check (spec `002-sandbox-quota-usage-tracking` FR-015–FR-019) MUST remain the sole quota-related gate at sandbox dispatch time — its own behavior (limit resolution, period handling, refusal shape, fail-mode) MUST NOT change as a result of this removal.
- **FR-003**: System MUST remove the platform-wide check itself, its dedicated exception type, its refusal-message formatting, and its call-recording function, along with the module-level logic that implements the platform-wide cap.
- **FR-004**: System MUST remove the deployment configuration variables that controlled the platform-wide limits (daily/weekly/monthly values and their independent fail-mode setting), including their entries in deployment documentation.
- **FR-005**: A deployment that still has the removed configuration variables set in its environment after upgrading MUST start normally — those variables MUST be silently ignored, not treated as a startup error.
- **FR-006**: System MUST remove the database table that persisted platform-wide usage counters by editing or deleting the migration that introduced it, directly at its source, rather than by adding a new, separate migration that reverses it — this is permissible specifically because that migration has never been merged into the main branch and therefore carries no released-deployment compatibility obligation under this project's migration-reversibility rule.
- **FR-007**: Removing that table MUST NOT alter or migrate the separate, unrelated usage ledger that spec 002's per-user tracking already uses — the two are independent tables and only the platform-wide one is removed.
- **FR-007a**: Any database that already applied the now-removed migration (this branch's own development/CI databases) MUST be reconciled as a one-time, local, manual step (dropping the table and re-pointing the recorded migration state to the new chain tip) — this reconciliation MUST NOT itself become a shipped migration step, since no real deployment ever received the migration being removed.
- **FR-008**: System MUST remove all tests that exist solely to exercise the removed platform-wide mechanism, rather than leaving them failing, skipped, or vacuously passing against dead code.
- **FR-009**: System MUST leave no orphaned references (dead imports, unreachable branches, stale documentation) to any of the removed functions, classes, configuration variables, or database objects.
- **FR-010**: Spec `002-sandbox-quota-usage-tracking` MUST be amended so its requirements, success criteria, and acceptance scenarios no longer describe the removed platform-wide cap as continuing to operate — without changing any requirement describing spec 002's own per-user combined enforcement.
- **FR-011**: Spec `003-sandbox-call-logging` MUST be amended so its requirement referencing the removed usage ledger no longer describes it as existing and unchangeable — without altering any requirement about the per-call structured logging spec 003 itself defines.

### Key Entities

- **Platform-Wide Sandbox Usage Counter** *(removed by this feature)*: The database-persisted, all-users-combined count of OpenSandbox-routed calls per configured period (day/week/month), previously used to enforce the platform-wide cap. Ceases to exist once this feature ships; superseded in practical effect by spec 002's per-user combined usage, which already tracks and enforces at the per-user level.
- **Per-User Combined Usage** *(unchanged, out of scope for this feature)*: Spec 002's existing agentic+sandbox usage ledger and enforcement ceiling — this feature does not modify it, it only removes the now-redundant sibling mechanism that used to sit alongside it.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of OpenSandbox-routed dispatch attempts, after this change ships, are gated by exactly one quota check (the per-user combined check) — 0% perform a platform-wide combined-usage check.
- **SC-002**: A fresh install's migration chain contains zero revisions that create or reference the removed table — the chain reads as though the platform-wide mechanism was never introduced.
- **SC-003**: The full test suite passes with zero remaining references to the removed platform-wide mechanism, and this branch's own development/CI databases are reconciled to the edited chain with no lasting drift.
- **SC-004**: Specs `002-sandbox-quota-usage-tracking` and `003-sandbox-call-logging` contain zero requirements, success criteria, or acceptance scenarios that depend on the removed platform-wide mechanism still existing.

## Assumptions

- No data-preservation or export step is needed for existing platform-wide usage-counter rows — this data is transient operational counters for a mechanism being deliberately retired, not business-critical or user-facing history.
- This is a pure removal/consolidation: no new user-facing capability is introduced, and spec 002's own per-user combined enforcement (FR-015–FR-019) is left functionally identical — only its now-redundant sibling mechanism is removed.
- Deployments that still set the removed configuration variables after upgrading experience no startup failure; those variables are simply no longer read by any code path, consistent with how unrecognized environment variables are already handled elsewhere in this codebase.
- The removal proceeds directly to full deletion in one change, rather than a deprecation warning period — appropriate given this is pre-GA, actively-developed functionality rather than a long-lived production contract with external consumers depending on a grace period.
- The migration that introduced the removed table has never been merged to the main branch — it exists only on this feature branch — so it carries no released-deployment compatibility obligation. It is therefore edited/removed directly at its source rather than reversed by a new counter-migration; any already-applied local/CI database is reconciled manually, once, as branch-local housekeeping rather than a shipped upgrade path.
- "OpenSandbox-routed dispatch" and "the three call sites" refer to the same code interpreter and two custom-tool (JavaScript/Python) execution paths already defined by specs 001 and 002 — this feature does not change which executions are in scope, only which checks run against them.
