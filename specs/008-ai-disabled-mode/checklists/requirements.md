# Specification Quality Checklist: AI-disabled platform mode

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

**All three clarifications resolved** on 2026-08-26, and written into FR-029, FR-030
and FR-031 rather than left as assumptions:

- **FR-029** — the MCP endpoint is off entirely, refusing with the standard reason and
  staying discoverable so a client is told rather than left guessing.
- **FR-030** — a flow step whose AI dependence is not clearly *no* counts as requiring
  AI. This is the answer with the widest reach: it makes "requires AI" the default for
  any step type nobody has classified yet, so the boundary cannot erode by omission.
- **FR-031** — no licence is required to run AI-disabled, and the mode is reported in
  telemetry.

The other five open questions were defaulted and recorded under Assumptions with the
reasoning: the CTEM dashboard and registration stay live, the public catalogue stays
readable, provider-dependent infrastructure is optional to run but never impossible
to add later, in-flight work finishes rather than being cancelled, and copy ownership
sits with product and marketing.

**Deliberately translated out of the issue's language.** The issue is written in
implementation terms — column names, file paths, function names, HTTP status codes.
The specification states the same requirements as observable behaviour, because the
mode has to be verifiable against what the platform does rather than against how it
is built. The issue remains the better reference for *where* the seams already exist,
and `/speckit-plan` should read it alongside this.

**One requirement is stronger here than in the issue.** The issue lists the
navigation, the badge and the landing screen as UI polish. FR-017 to FR-020 make them
requirements, and User Story 3 is P1, because an install that is silently missing half
its function is the problem being solved — a correct backend behind a confusing screen
would not deliver the feature.
