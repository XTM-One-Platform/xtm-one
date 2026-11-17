# Specification Quality Checklist: Sandbox Usage Quota Tracking

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-07-31
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

- Source GitHub issue was already well-scoped with explicit acceptance criteria, which made reasonable defaults available for all open questions (attribution, success-only counting, scope of "sandbox" to the managed/OpenSandbox path). No [NEEDS CLARIFICATION] markers were needed.
- All items pass on the first validation pass.
- 2026-07-31 `/speckit-clarify` session resolved two additional ambiguities not covered by the source issue: (1) fail-open behavior when the usage-recording step itself fails, and (2) restart-required (not runtime-dynamic) semantics for the counting toggle, correcting an inconsistency introduced during `/speckit-specify` (SC-002 and User Story 2's acceptance scenario had implied no-restart-needed, which didn't match this codebase's settings model). All items still pass after integration.
- 2026-07-31, a later `/speckit-clarify` session (run after `/speckit-implement` had already shipped code against the original monthly-only design) added FR-011: sandbox usage periods now resolve with the same user > group > platform > monthly-default precedence as LLM-proxy/agentic usage, instead of a fixed period. This decision postdates the current implementation — the code has not yet been updated to match. All checklist items still pass on the spec itself after integration.
- 2026-07-31, a third `/speckit-clarify` session (run after FR-011's own implementation had already shipped) added FR-012: sandbox usage totals must appear in the existing Prometheus `/metrics` endpoint, unconditionally across platform modes — partially reversing research.md §7's earlier "deferred" framing, but only for this one display surface (the per-user `get_user_quota_usage` display remains a documented follow-up, unchanged). This decision also postdates the current implementation. All checklist items still pass on the spec itself after integration.
- 2026-07-31, a fourth `/speckit-clarify` session (run after FR-012's own implementation had already shipped) added FR-013/FR-014: fully reverses the remaining part of research.md §7's deferral — sandbox usage must now also appear in `get_user_quota_usage`'s returned data AND be visibly displayed on every existing frontend page that already shows agentic/LLM-proxy usage (profile, user detail, group detail, platform quota settings), discovered during this session to each hardcode their own usage-type list independent of the backend. FR-014 additionally requires the displayed entry not imply an enforced limit that doesn't exist, since sandbox usage has no configurable cap. This decision also postdates the current implementation. All checklist items still pass on the spec itself after integration.
- 2026-07-31, a fifth `/speckit-clarify` session (run after FR-013/FR-014's own implementation had already shipped a dedicated "Sandbox Executions" row on all four pages) reversed the display approach from that same day: FR-013/FR-014 now require sandbox usage to be folded into the displayed "Agentic Executions" count on profile/user detail/platform quota settings (display-only — the ledger, period resolution, Prometheus `/metrics`, and agentic's own enforcement all keep sandbox and agentic distinct), with no sandbox row on group detail at all. This decision also postdates the current implementation, which will need to be reworked to match. All checklist items still pass on the spec itself after integration.
- 2026-08-03, a sixth `/speckit-clarify` session reversed the enforcement half of that same-day-prior FR-014 decision: agentic's *enforced* limit now also counts sandbox usage (added FR-015–FR-019), sharing the existing agentic per-user limit as a combined ceiling, checked at both existing per-user agentic checkpoints plus a new checkpoint added at sandbox-dispatch time (previously platform-wide-cap-only). This decision postdates the current implementation — both existing checkpoints enforce agentic-only usage today, and no combined check exists at sandbox dispatch — all three will need to be built/updated to match. All checklist items still pass on the spec itself after integration.
- 2026-08-03, a seventh `/speckit-clarify` session revised FR-011 itself: the independently-resolved sandbox period it introduced (2026-07-31) turned out to have no remaining purpose once the FR-015–FR-019 combined check always reads sandbox usage under agentic's period regardless (FR-017), and it was never exposed in any admin UI. Worse, it let the combined check's read key and `increment_sandbox_usage`'s write key drift apart whenever an admin configured the two periods differently — a real undercounting bug. Sandbox now always uses agentic's resolved period for per-user enforcement/display; the `QuotaConfig.sandbox` field and `resolve_quota_config`'s "sandbox" recognition (both added by the original FR-011) are removed entirely, since nothing is left for either to carry. The platform-wide Prometheus/monitoring row keeps a fixed monthly bucket instead of trying to inherit a single "the" agentic period (which doesn't exist in XTM One mode, where agentic is scanned under up to three periods at once). This decision postdates the current implementation — the code still resolves an independent sandbox period and still has the `QuotaConfig.sandbox` field — both will need to be removed to match. All checklist items still pass on the spec itself after integration.
