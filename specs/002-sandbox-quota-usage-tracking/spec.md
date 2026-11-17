# Feature Specification: Sandbox Usage Quota Tracking

**Feature Branch**: `002-sandbox-quota-usage-tracking`

**Created**: 2026-07-31

**Status**: Draft

**Input**: User description: "feat(sandbox): increment quota_usage on sandbox calls, gated by a config flag"

## Clarifications

### Session 2026-07-31

- Q: If the usage-recording step itself fails (e.g., a transient outage of the datastore backing usage counters) at the moment a sandbox call has already completed successfully, what should happen to that sandbox call? → A: Fail-open for recording only — the sandbox call still succeeds and returns its result; the failed usage-count increment is logged as an error but not retried, so that one execution may be undercounted.
- Q: Should turning the sandbox-usage-counting setting on/off require a backend restart/redeploy, or must it take effect immediately without one? → A: Restart required — a standard environment-variable setting, matching every other on/off toggle already used in this codebase's configuration.
- Q: Should the period used to count sandbox usage (daily/monthly/yearly) be resolvable per-user/group like it already is for LLM-proxy and agentic usage, or always fixed? → A: Same mechanism as LLM-proxy/agentic — resolved with the same precedence (user-specific, then group, then platform-wide default, then a fixed monthly default when nothing is configured), not a single fixed period.
- Q: Should sandbox usage totals appear in the existing Prometheus `/metrics` endpoint, and if so, should that exposure be gated by platform mode the way LLM-proxy usage is there, or unconditional like agentic usage? → A: Unconditional, like agentic usage — sandbox usage tracking behaves identically across every platform mode, so its visibility in `/metrics` should not depend on platform mode either.
- Q: Should adding sandbox usage to the per-user usage-retrieval mechanism also extend to the user-facing/admin-facing usage displays (profile, user detail, group detail, platform quota settings), or stay backend-only? → A: Backend and frontend — sandbox usage should be visibly displayed as its own entry everywhere the existing usage-type entries (agentic, LLM-proxy) are already displayed, not just returned by the underlying data-retrieval mechanism without being shown anywhere.
- Q: Reconsidering the prior answer — should sandbox usage instead be visible without being its own identifiable line in the usage UI, folded into the agentic usage count wherever a usage count is displayed? → A: Display-only merge — the profile, user detail, and platform quota settings pages show sandbox usage added into the "Agentic Executions" count as a single number, with no separate sandbox row. The underlying usage ledger, the per-type period resolution, the Prometheus `/metrics` endpoint, and agentic's own enforced limit/blocking behavior all remain unaffected and keep sandbox and agentic fully distinct — only the display layer merges the two counts.
- Q: Should the group detail page's sandbox row (a read-only "Configured"/"Not set" period indicator, not a usage count) also be folded into the agentic row, or removed? → A: Removed entirely — there is no usage count on that row to aggregate with agentic's, and group-level sandbox period configuration has no user-facing display anywhere once removed.

### Session 2026-08-03

- Q: Should the display-only merge of agentic and sandbox usage (FR-013/FR-014) be extended so agentic's *enforced* limit also counts sandbox usage, and if so where? → A: Everywhere a per-user agentic quota check already happens — the interactive agent-chat check and the assignment/schedule-triggered agent-run check — plus a brand-new per-user check added at the point sandbox executions are dispatched (code interpreter, custom JS tool, custom Python tool), which today only enforces the platform-wide cap from spec `001-sandbox-call-quotas`.
- Q: What limit should the combined agentic+sandbox usage be checked against, given sandbox has no configurable limit of its own? → A: The existing per-user agentic limit (already resolved via user → group → platform-wide precedence) — no new, separately configurable "combined" quota type or admin UI surface.
- Q: Agentic and sandbox usage periods resolve independently (FR-011) and can differ for the same user — which period window should the combined check sum over? → A: Always the agentic limit's resolved period window; sandbox usage is looked up under that same window for this calculation only. Sandbox's own independently-resolved period is unaffected and continues to govern its own display/metrics rows.
- Q: When the new sandbox-dispatch check refuses a call, how should that refusal surface, given sandbox dispatch happens outside interactive chat too? → A: As a soft refusal result to the caller (not a hard error), mirroring how an agentic quota refusal is already surfaced as an in-conversation message during interactive chat, so chat-triggered and non-chat-triggered dispatches degrade the same way.
- Q: If the usage-tracking backend is unreachable at sandbox-dispatch time, which existing fail-mode setting governs whether the call proceeds? → A: The existing agentic-quota fail-mode setting (the same one the interactive chat agentic check already respects) — not the separate sandbox-specific fail-mode setting, and no new setting is introduced, since this is still fundamentally the agentic check, now also reading sandbox counters.
- Q: FR-011's independently-resolved sandbox period (distinct from agentic's) turned out to have no remaining purpose — the combined check always reads sandbox usage under agentic's period anyway (FR-017), and per-user/group sandbox period configuration has no UI surface anywhere (the group detail page's own indicator was already removed, per the prior session's clarification). Should sandbox keep resolving its own period at all? → A: No — drop it entirely. Sandbox usage always uses the same period as the user's agentic quota, for both enforcement and per-user display. The platform-wide Prometheus/monitoring series (FR-012), which has no single "the" agentic period to inherit (XTM One mode scans agentic under up to three periods at once), keeps its own fixed monthly bucket instead of trying to track one.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Sandbox usage counts toward per-user quota by default (Priority: P1)

As a platform operator, I want every user's sandbox execution to be reflected in that user's overall usage totals — alongside their LLM and agent usage — so that quota enforcement and usage reporting account for all the resources a user actually consumes, without requiring any setup.

**Why this priority**: This is the core value of the feature. Today, sandbox executions are invisible in per-user usage reporting — only LLM-proxy and agentic usage are tracked per user. Without this, operators cannot see or act on the fact that a user's sandbox consumption is part of their overall footprint. This must work out of the box, with no configuration required, to be useful.

**Independent Test**: Can be fully tested by having a user trigger a sandbox execution (e.g., running code via the code interpreter or a custom tool) and confirming their usage totals for the current period increase, without touching any configuration.

**Acceptance Scenarios**:

1. **Given** a deployment with default settings, **When** a user's code interpreter execution completes successfully in the sandbox, **Then** that user's usage total for the current period includes one additional sandbox usage unit.
2. **Given** a deployment with default settings, **When** a user runs a custom tool (JavaScript or Python) that executes in the sandbox, **Then** that user's usage total for the current period includes one additional sandbox usage unit, regardless of which of the two custom-tool paths was used.
3. **Given** a user's sandbox execution fails or errors during execution, **When** the failure occurs, **Then** that attempt does not add to the user's usage total.

---

### User Story 2 - Operators can turn off sandbox usage counting (Priority: P2)

As an operator running a self-hosted/on-prem deployment, I want to disable sandbox usage counting so that sandbox calls in my environment do not count against my users' quotas — since I manage and pay for my own sandbox infrastructure independently of any hosted usage-based billing.

**Why this priority**: Without an opt-out, self-hosted operators would have sandbox usage counted against quotas designed for a hosted/billed context, which doesn't apply to them. This is a necessary safety valve but is secondary to the core tracking behavior (P1) — most deployments should get counting by default.

**Independent Test**: Can be fully tested by changing a single deployment-level setting, then confirming that subsequent sandbox executions by any user no longer change that user's usage totals, while sandbox executions themselves continue to work unchanged.

**Acceptance Scenarios**:

1. **Given** an operator has turned off sandbox usage counting for their deployment, **When** any user successfully executes code or a custom tool in the sandbox, **Then** that user's usage total does not change.
2. **Given** sandbox usage counting is turned off, **When** any user successfully executes code or a custom tool in the sandbox, **Then** the combined agentic+sandbox quota check (FR-015–FR-019) evaluates that user's agentic usage alone, since no sandbox usage was ever recorded to sum in — the same degrade-to-agentic-only behavior already described in Edge Cases.
3. **Given** an operator turns sandbox usage counting back on and restarts/redeploys the backend (the standard way configuration changes take effect in this deployment), **When** the next sandbox execution completes successfully after that restart, **Then** it is counted, with no database migration or other maintenance action needed.

---

### User Story 3 - Sandbox usage is reported consistently with existing usage types (Priority: P3)

As an administrator who already reviews usage reports for LLM and agent activity, I want sandbox usage to be reflected in that same usage ledger and in the numbers I already look at, so I don't need to learn or consult a separate system to understand a user's total consumption — without cluttering the usage UI with an additional line item for a usage type that has no enforceable limit of its own.

**Why this priority**: This is about consistency and reducing operator burden rather than introducing new capability. At the data and platform-metrics layer, it ensures sandbox usage integrates cleanly as its own distinguishable usage type, the same way agentic and LLM-proxy usage already do. At the user-facing display layer, it deliberately does the opposite of "add a new line" — sandbox usage is folded into the existing "Agentic Executions" count wherever a usage count is already shown, so administrators and users see one consolidated number instead of a growing list of usage types with no separately displayed limit.

**Independent Test**: Can be fully tested by inspecting a user's recorded usage entries after a sandbox execution and confirming the sandbox entry is recorded as its own distinguishable usage type in the ledger, alongside (not mixed into) any existing LLM-proxy or agentic entries for the same user and period; by confirming sandbox usage totals appear in the existing Prometheus metrics endpoint's output as their own series; and by confirming that on the profile, user detail, and platform quota settings pages, the displayed "Agentic Executions" count includes the user's (or platform's) sandbox usage added in, with no separate sandbox row anywhere in the UI.

**Acceptance Scenarios**:

1. **Given** a user has both LLM-proxy usage and sandbox usage in the same period, **When** their usage is reviewed at the data layer (ledger, Prometheus metrics), **Then** the two are recorded as distinct, separately identifiable usage types.
2. **Given** a user has never used the sandbox, **When** their usage is reviewed, **Then** no sandbox usage entry is created for them (no zero-value clutter), and the displayed agentic count is unaffected.
3. **Given** any platform mode configuration, **When** the existing Prometheus metrics endpoint is queried, **Then** its output includes sandbox usage totals alongside the existing agentic usage totals, unconditionally, as its own series — not gated by platform mode, and not merged with agentic at this layer.
4. **Given** a user has recorded sandbox usage, **When** an administrator views that user's profile or user detail page, or an administrator views the platform quota settings page, **Then** the existing "Agentic Executions" count/tile shown there includes that sandbox usage added into the number — with no separate, identifiable sandbox row anywhere in that UI.
5. **Given** sandbox usage has no configurable enforceable limit of its own (unlike LLM-proxy/agentic usage, which can have an admin-configured cap), **When** a user's combined agentic + sandbox usage for the current period reaches their agentic limit, **Then** further agent chat turns, assignment-triggered agent runs, and sandbox dispatches for that user are all refused until the period resets — the displayed merged number reflects the actual shared enforcement ceiling, not a display-only convenience (see FR-015–FR-019).
6. **Given** a group's `quota_config` has a sandbox period configured, **When** an administrator views that group's detail page, **Then** no sandbox-specific row is displayed there (it was a configuration indicator, not a usage count, so there is nothing to merge into agentic's group-level display).

### Edge Cases

- What happens when a sandbox execution's outcome is ambiguous (e.g., it times out rather than cleanly succeeding or failing)? Only clearly successful executions should count, consistent with the "successful calls only" rule.
- What happens if the usage-counting setting is changed mid-period (e.g., turned off partway through a month)? Only calls made after the change should be affected; usage already recorded before the change is not retroactively altered.
- What happens if a sandbox call cannot be attributed to a specific user (e.g., a system-initiated call with no owning user in context)? Such calls are not counted toward any per-user total, since there is no user to attribute them to.
- What happens when a user is already at or over their quota from other usage types and then performs a sandbox execution? This feature only adds sandbox executions to the recorded usage total — it does not change how enforcement/blocking decisions are made once a user is over quota (that is existing, unchanged behavior).
- What happens if the usage-recording step itself fails (e.g., a transient outage of the datastore backing usage counters) right after a sandbox execution has already completed successfully? The sandbox execution's result is still returned to the user; the failed usage increment is logged as an error and not retried, so that one execution may go undercounted rather than the user's already-completed work being blocked or reported as failed.
- What happens when a user has heavy sandbox usage but little or no agentic usage of their own, or vice versa? Because the agentic limit is now a shared ceiling for the sum of both (FR-015–FR-017), heavy sandbox consumption alone can block that user's next agent chat turn or assignment-triggered run even with zero agentic usage, and heavy agentic usage alone can block their next sandbox dispatch — this is expected, not a bug.
- What happens to the combined check when an operator has disabled sandbox usage counting entirely (FR-006)? No sandbox usage is ever recorded to sum in, so the combined check naturally degrades to agentic-only usage — no special-casing is required.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST record one unit of usage against the initiating user's usage total for each sandbox execution that completes successfully.
- **FR-002**: System MUST NOT record usage for sandbox executions that fail or error out.
- **FR-003**: System MUST distinguish sandbox usage from other existing usage types (LLM-proxy, agentic) recorded for the same user, so each can be identified and reported on separately.
- **FR-004**: System MUST record sandbox usage for every feature that dispatches an execution to the sandbox (the code interpreter and both custom-tool execution paths), with consistent behavior across all of them.
- **FR-005**: System MUST count sandbox usage by default, with no configuration action required for a deployment to get this behavior.
- **FR-006**: System MUST provide a single deployment-level setting that operators can use to turn sandbox usage counting off entirely.
- **FR-007**: When the setting is off, System MUST NOT record any sandbox usage against per-user totals, while still allowing sandbox executions themselves to proceed normally.
- **FR-008**: Turning the setting on or off MUST take effect for subsequent sandbox executions after the standard configuration-change process for this deployment (a restart/redeploy), without requiring a database change or migration.
- **FR-009**: *(Superseded by spec `004-remove-sandbox-call-quota`)* This feature originally required that a separate, platform-wide (all-users-combined) sandbox call cap (spec `001-sandbox-call-quotas`) keep operating unchanged, independently of this feature's per-user counting. That platform-wide cap has since been removed entirely as redundant with this feature's own combined agentic+sandbox enforcement (FR-015–FR-019) — there is no longer a separate mechanism for this feature to avoid disturbing.
- **FR-010**: If recording a sandbox execution's usage fails for any reason (e.g., a transient infrastructure outage), System MUST still return the sandbox execution's result to the user; the recording failure MUST be logged but MUST NOT cause the sandbox call to fail, be retried, or be reported as unsuccessful.
- **FR-011**: *(Revised 2026-08-03 — see Clarifications)* Sandbox usage has no configurable period of its own for per-user enforcement or display: it always uses the same period as the user's resolved agentic quota (FR-017). System MUST NOT resolve a separate, independently-configured sandbox period for these purposes. The platform-wide Prometheus/monitoring series (FR-012) is a separate, global-only observability row with no single "the" agentic period to inherit — it MUST use a fixed monthly bucket.
- **FR-012**: System MUST expose sandbox usage totals through the existing platform-wide Prometheus metrics endpoint, unconditionally across all platform mode configurations — consistent with how agentic usage totals are already exposed there, and not gated by platform mode the way LLM-proxy usage totals are on that same endpoint.
- **FR-013**: System MUST include sandbox usage in the per-user/per-group usage data returned for display, and on the profile, user detail, and platform quota settings pages, the count already displayed for agentic usage MUST include the corresponding sandbox usage added into it — sandbox MUST NOT appear as its own separate, identifiable row on any of those pages. The group detail page MUST NOT display any sandbox-related row (its existing sandbox indicator there is a configuration/period display, not a usage count, so there is nothing to merge).
- **FR-014**: Sandbox usage has no configurable enforceable limit of its own (unlike LLM-proxy/agentic usage) — it shares the agentic per-user limit as its enforcement ceiling (FR-015–FR-017). The displayed merged "Agentic Executions" count therefore reflects what agentic enforcement actually evaluates, not a display-only convenience.
- **FR-015**: System MUST check the sum of a user's agentic usage and sandbox usage for the current period against that user's agentic per-user limit at every point that today checks agentic usage alone — the interactive agent-chat check and the assignment/schedule-triggered agent-run check — and additionally, newly, at the point any sandbox execution is dispatched (code interpreter, custom JS tool, custom Python tool).
- **FR-016**: The combined check MUST use the existing per-user agentic limit (resolved via the existing user → group → platform-wide precedence for agentic usage) as its enforcement ceiling. System MUST NOT introduce a new, separately configurable "combined" quota type or limit for this purpose.
- **FR-017**: When resolving usage for the combined check, sandbox usage MUST be counted within the same period window as the agentic limit being enforced — which is also the only period sandbox usage is ever recorded under for per-user purposes (FR-011, revised); the platform-wide monitoring series (FR-012) is unaffected, since it never shared this window in the first place.
- **FR-018**: When the combined check refuses a sandbox dispatch because the shared ceiling is reached, the refusal MUST be surfaced to the caller as a normal outcome/result (a soft refusal), not as a hard error — mirroring how an agentic quota refusal is already surfaced as an in-conversation message during interactive chat — so that chat-triggered and non-chat-triggered sandbox dispatches degrade the same way.
- **FR-019**: If the usage-tracking backend is unreachable when a sandbox dispatch performs this check, the existing agentic-quota fail-mode setting (not the separate sandbox-specific fail-mode setting, and no new setting) MUST govern whether the call proceeds (fail-open) or is refused (fail-closed), consistent with how the agentic check already behaves at its existing enforcement points.

### Key Entities

- **Usage Record**: A per-user, per-usage-type, per-period count of quota-relevant actions taken by that user. Already exists for LLM-proxy and agentic usage; this feature adds sandbox executions as a new, distinct usage type recorded in the same place. Unlike LLM-proxy and agentic, sandbox has no configured period of its own — for per-user purposes it is always recorded under the same period as the user's agentic usage (FR-011, revised), and it is exposed through the same platform-wide metrics endpoint as its own series, under a fixed monthly bucket (FR-012). It is deliberately *not* given its own row in the user-facing display layer — it is folded into the displayed agentic count instead (FR-013/FR-014). It is also folded into agentic's own enforcement: it has no limit of its own and instead shares the agentic per-user limit as a combined ceiling (FR-015–FR-019).
- **Sandbox Execution**: A single run of user- or agent-triggered code (via the code interpreter or a custom tool) that takes place inside the sandbox environment, initiated on behalf of a specific user.
- **Sandbox Usage Counting Setting**: A deployment-wide on/off configuration value that determines whether sandbox executions are added to per-user usage totals.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of successful sandbox executions across all sandbox-triggering features are reflected in the initiating user's usage totals by default, with zero configuration needed.
- **SC-002**: An operator can fully disable sandbox usage counting for their deployment with a single configuration change and a standard restart/redeploy, with no database migration or other maintenance action needed.
- **SC-003**: After disabling the setting, 0% of subsequent sandbox executions appear in any user's usage totals, while 100% of them still execute normally.
- **SC-004**: *(Superseded by spec `004-remove-sandbox-call-quota`)* This success criterion originally measured the separate platform-wide sandbox call cap (spec `001-sandbox-call-quotas`), which has since been removed entirely — see SC-007 for this feature's own combined-ceiling success criterion instead.
- **SC-005**: Sandbox usage totals appear in the existing Prometheus metrics endpoint's output, as their own distinguishable series, in 100% of platform mode configurations.
- **SC-006**: A user with recorded sandbox usage sees it reflected in the displayed "Agentic Executions" count on 100% of the profile, user detail, and platform quota settings pages, with no separate, identifiable sandbox row appearing anywhere in that usage-review UI (including group detail, which shows no sandbox-related row at all).
- **SC-007**: Once a user's combined agentic + sandbox usage for the current (agentic-resolved) period reaches their agentic limit, 100% of that user's subsequent agent chat turns, assignment-triggered agent runs, and sandbox dispatches are refused until the period resets, in every case where the quota backend is reachable (or the existing agentic-quota fail-mode is set to fail-closed when it is not).

## Assumptions

- Only successful sandbox executions are counted; failed, errored, or ambiguous-outcome executions are not, matching the explicit acceptance criteria in the source issue.
- Each sandbox execution occurs within a broader execution flow (the code interpreter or custom-tool invocation) where the initiating user is known and available — even though the specific low-level functions that dispatch to OpenSandbox do not themselves receive it directly, requiring it to be threaded down from their callers. This mirrors how existing agentic usage is already attributed within that same broader flow, not at the identical function.
- This feature covers only the sandbox executions routed through the platform's managed sandbox environment (the code interpreter and the two custom-tool execution paths). Sandbox executions run through the separate local/non-managed execution path are out of scope.
- The counting setting is a single deployment-wide toggle (not configurable per user, per group, or per agent), implemented as a standard environment-variable configuration value; changes take effect on the next restart/redeploy, consistent with how every other on/off toggle in this codebase already behaves.
- No database schema change is required, since the underlying usage ledger already accepts new usage-type values without a migration.
- *(Superseded by spec `004-remove-sandbox-call-quota`)* This feature originally assumed a separate, still-existing platform-wide sandbox call counter (spec `001-sandbox-call-quotas`) that was unaffected by this feature in either direction. That counter and its whole mechanism have since been removed entirely.
