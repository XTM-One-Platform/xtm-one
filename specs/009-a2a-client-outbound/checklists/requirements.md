# Specification Quality Checklist: Minimal A2A Client (Outbound)

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-08-26
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

### Iteration 1 — 2026-08-26

Three `[NEEDS CLARIFICATION]` markers raised, each because it changed what
would be built rather than how: the phase's scope boundary (Context), the
supported partner platforms (FR-009), and the treatment of an unsigned partner
card (FR-016).

### Iteration 2 — 2026-08-26 (all items pass)

All three answered and folded in as `## Clarifications → Session 2026-08-26`.
Eleven edits followed from the answers rather than just marker removal, because
each decision had consequences elsewhere in the document:

- Delegation confirmed in scope; the scope sentence now states the boundary
  positively ("one binding, one delegation, one result") so that a single
  exchange does not read as a starting point.
- **FR-045** added, naming both interoperability targets and their distinct
  credential flows, and requiring identical delegation behaviour across them.
  Numbered at the next free value, following the convention
  `specs/006-a2a-server-inbound` used for its later additions.
- **FR-016** inverted: an unsigned card no longer blocks registration, and no
  per-registration acknowledgement gate exists. **FR-017** was repurposed from
  invalidating a trust decision to requiring that "signature-verified" versus
  "accepted unverified" is recorded and surfaced per registration *and* per
  delegation — the honesty obligation the chosen answer carries.
- **FR-019** now treats the loss of a previously present signature as a
  material change, so a partner that stops signing suspends delegation.
- Consequential fixes so nothing still describes withdrawn machinery: FR-039's
  audit list, two edge cases, the Partner Agent Registration entity, SC-008,
  SC-013, a new SC-016, User Story 2's third acceptance scenario, and the F7
  assumption — which now states plainly that F7 is answered in full only for a
  card that carries a signature.

**Counts at end of iteration 2**: 4 user stories (3×P1, 1×P2), 45 functional
requirements, 16 success criteria, 20 edge cases.

### Iteration 3 — 2026-08-26 (`/speckit-clarify`, 5 questions)

All 16 items still pass; no checkbox state changed. Two items are more strongly
satisfied than they were, because the five answers replaced the unquantified
adjectives they had been passing on: the re-verification cadence
("periodically"), the delegation ceiling ("a bounded, configured share"), the
breaker cooldown ("a period"), and SC-008's undefined "one verification cycle".
Each now carries a stated default.

Also corrected here: User Story 4's narrative, independent test and first
acceptance scenario still measured against "one verification cycle" after the
freshness answer replaced that unit — an invalidated statement, so it was
rewritten rather than left alongside the new rule.

**Counts now**: 4 user stories, 53 functional requirements, 19 success
criteria, 26 edge cases, 8 clarification bullets. Every FR is a MUST statement;
every SC carries a quantity; FR and SC ids are unique.

### Deliberate exceptions, recorded rather than fixed

- **The spec names the platform's own existing mechanisms** (integrations
  catalog, integration bindings, egress policy, prompt-injection defenses,
  connectivity verdicts) and cites prior artefacts (benchmark findings
  F1–F7, `specs/006-a2a-server-inbound`). These are *reuse constraints* — the
  premise of the feature request — not implementation choices, and the
  minimality argument is unreadable without them. Two provider keys and one
  credential scope appear for the same reason: they identify which existing
  partner mechanism each decision is symmetric with. No language, framework,
  library, endpoint shape, schema or code structure appears.
- **`SC-014` (zero-call-site sweep) is a process criterion**, not a user-facing
  outcome. Retained deliberately: it is the specific guard the server phase's
  2026-08-19 implementation review named for this phase, and dropping it would
  discard the one lesson that review asked to be carried forward. **FR-021**,
  **FR-031** and **FR-049** are written to the same end — every run path and
  every partner state the spec names must be reachable through the ordinary
  delegation path, rather than existing as branches for states the
  implementation cannot produce.

### Iteration 4 — 2026-08-27 (`/speckit-analyze` remediation)

All 16 items still pass; no checkbox state changed. `/speckit-analyze` found 14 issues across the four artifacts and all were fixed. One was CRITICAL and is recorded as a reversal in the spec's own Clarifications (Session 2026-08-27): **FR-035 named an egress policy that does not exist on this code path**, so it is replaced by a scoped destination guard plus a new **FR-054** that classifies a deployment-level network refusal. Two HIGH findings were defects in the plan-phase design docs rather than the spec — `A2ADelegation.run_ref` was an untyped uuid where the sibling inbound table already had a real foreign key, and `signature_state` / `usability` were NOT NULL with no defined value for a row that exists before its first verification. A third supplied the missing default for FR-027's response size bound.

Two items are more strongly satisfied than before: "requirements are testable" (FR-027 now has a number; FR-035 now describes something implementable) and "dependencies and assumptions identified" (registration uniqueness and the outbound rate-limiting asymmetry are now stated rather than left open).

**Counts now**: 4 user stories, **54** functional requirements, 19 success criteria, 26 edge cases, 9 clarification bullets across two sessions. 80 tasks, of which 1 is already complete. Every FR is a MUST statement; every SC carries a quantity; all 54 FRs and 19 SCs are cited by at least one task; no cross-artifact reference dangles.

### Iteration 5 — 2026-08-27 (Principle XI applied)

Constitution v2.1.0 added **XI. Minimal, Reviewable Writing**; the spec was rewritten against it. 33% shorter by word count (10,054 → 6,728) with nothing removed that carries an obligation: all 54 FRs, 19 SCs, 22 headings and 22 acceptance scenarios survive, and no citation in the other eight artifacts dangles.

What went: deliberation transcripts in the nine Clarifications bullets, now decision plus one deciding reason plus named rejected options (longest is 81 words, was ~200); rationale sentences inside requirements that restated Assumptions or Clarifications; eleven Edge Cases that restated a requirement without adding a case; two Context paragraphs of scope ceremony; and one duplicated Assumptions bullet — signing-vs-verification was stated twice, introduced during the iteration-4 remediation and caught by XI's one-fact-one-place rule.

Two obligations moved rather than being cut, and both were verified in their new home: FR-021's "no run path admitted that the implemented path cannot serve" consolidated into FR-049, which already made the same claim about partner states. FR-017's audit-record surface was lost in the first pass and restored — the compressed wording covered the registration and the delegation but silently dropped the third surface.

**Counts now**: 4 user stories, 54 functional requirements, 19 success criteria, 15 edge cases, 9 clarification bullets across two sessions. 80 tasks, 1 complete.

### Iteration 6 — 2026-08-27 (`/speckit-clarify`, 4 questions on integration direction)

All 16 items still pass; no checkbox state changed, but one was at risk: the four new requirements (FR-055–FR-058) arrived with no acceptance scenario, which would have failed "all functional requirements have clear acceptance criteria". Two scenarios were added to User Story 1 rather than letting the item regress.

The feature's control plane changed shape. An A2A integration now carries **exactly one direction**, inbound or outbound; the existing two inbound mechanisms become direction-switchable; a third entry is added for Google Agent Platform outbound, which neither existing credential flow reaches. Outbound OAuth became a third supported target, its partner being anything that accepts client-credentials — this platform's own inbound surface included. Changing direction is refused while an integration is bound.

This puts the inbound exposure model in scope, which the spec previously excluded: the shipped hook creates an exposure for any A2A binding, so without teaching it about direction, binding an integration for outbound purposes would silently expose the agent.

**Counts now**: 4 user stories, 58 functional requirements, 19 success criteria, 17 edge cases, 13 clarification bullets across three sessions.

### Iteration 7 — 2026-08-27 (plan and tasks regenerated)

All 16 items still pass; no checkbox state changed. `/speckit-plan` and `/speckit-tasks` were re-run against the direction decision, so the staleness iteration 6 flagged is cleared: no artifact now describes the superseded two-provider shape.

Four findings came from re-reading shipped code. `binds_a2a` tests provider-key membership alone, so it must become direction-aware or an outbound binding exposes the agent. The exposure hook observes `Agent` instances only and never sees an integration change — which the refuse-while-bound answer turns from a gap into a saving, since no second listener is needed. The catalog has no conditional-field support at all, so per-direction fields are a new entry shape. And this platform serves its own cards anonymously, so FR-014's universal "authenticated card retrieval" is false for the third target — logged as T004, the same class of defect as the egress one.

**Counts now**: 58 functional requirements, 19 success criteria, 4 user stories, **90 tasks** (was 80). All 58 FRs and 19 SCs are cited by at least one task; no cross-artifact reference dangles.

### Iteration 8 — 2026-08-27 (`/speckit-analyze` remediation)

All 16 items still pass; no checkbox state changed. Nine findings were raised and all nine fixed.

The CRITICAL one was the inverse of iteration 4's: not a requirement the code could not satisfy, but a **behaviour with tests and no acceptance criterion**. Nothing measured "an outbound binding creates no exposure" — the fail-open regression this feature's shipped-code change can cause. **SC-020** now does, and names every path reaching the exposure hook, including creating an agent with bindings already set, which `_sync_new_agent_exposure` serves separately from the edit path. For a phase whose inherited lesson is that passing tests can read as assurance, a security property with tests but no criterion was the right shape to catch.

Three HIGH findings sat in one place: Key Entities was never updated for direction. It named a field FR-009 had removed, omitted direction entirely, and described binding as unconditionally conferring delegation. That section is the fastest part of the spec to read and was the most wrong.

FR-014 was also corrected — card retrieval presents credentials *where the target requires them*, since this platform serves its own cards anonymously — closing T004 and the follow-ups it had left in research.md and the partner-invocation contract.

**Counts now**: 58 functional requirements, **20** success criteria, 4 user stories, 90 tasks (1 complete). All 58 FRs and 20 SCs cited by at least one task; no cross-artifact reference dangles.

Ready for `/speckit-implement`.
