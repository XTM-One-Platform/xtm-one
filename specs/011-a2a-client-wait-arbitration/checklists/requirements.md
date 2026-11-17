# Specification Quality Checklist: How a calling agent waits for a partner's answer

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-09-09
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

- Items marked incomplete require spec updates before `/speckit-clarify` or `/speckit-plan`

### Validation record

One pass, no failures. Checked by reading and by scanning for the two things
this spec was most likely to leak: a named transport or protocol method in the
streaming and callback requirements, and a duration in seconds in the success
criteria. Neither appears — FR-011 and FR-016 state the obligation and leave
the mechanism to the plan, and SC-001 refers to the inbound surface's own
maximum task lifetime rather than the number configuring it.

Two points were left open here and both were settled by `/speckit-clarify`
(see the spec's Clarifications section):

- When a still-running outcome is returned. FR-001 no longer waits for the
  ceiling where a callback will report the outcome; a short grace period ends
  the attempt instead, because this platform's own inbound surface answers
  every submission before the work is done.
- How callback reachability is decided. FR-010 now names an explicit operator
  setting, falling back to derivation from the configured public address.

### Cross-spec obligations

This spec amends two things in `009-a2a-client-outbound`, which `/speckit-plan`
must carry into tasks:

- FR-024 — a ceiling reached against a live task is no longer a timeout, and no
  longer cancels (this spec's FR-001)
- The Out-of-scope entry *"A new partner-facing surface for a partner to poll or
  push into."* — US3's callback receiver is one. Found during `/speckit-plan`'s
  post-design constitution re-check, not during specification

It also extends `009` FR-025 (cancel on run teardown) to tasks left outstanding
by a still-running outcome. That gap exists today independently of this feature:
teardown reaches only tasks a partner reported `input-required` for, so a task
merely still working is not cancelled when its caller dies.
