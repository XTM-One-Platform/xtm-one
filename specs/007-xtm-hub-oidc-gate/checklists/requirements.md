# Specification Quality Checklist: XTM Hub sign-in entitlement gate

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-08-19
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

- The **claim contract was pinned on 2026-08-19** against a live XTM Hub token.
  It inverted the original assumption: the Hub
  emits one claim *per instance*, named after the instance, so entitlement is
  the claim's **presence** rather than a value to compare. FR-001 and FR-004
  were rewritten accordingly, and FR-004a was added to keep the wider
  organisational group claim out of scope deliberately rather than by omission.

- Two requirements were **not** in the original ask and were added from the
  constitution's *Fail-Closed by Default* principle and from reviewing the
  sign-in code: FR-011 (a gate that cannot be evaluated must fail loudly rather
  than refuse everyone silently) and FR-012 (keep the non-federated route open
  so a misconfiguration is recoverable). Both are safeguards against locking an
  operator out of their own instance; flag them at planning if they are unwanted.

- FR-005/FR-006 sit in tension by design — central role management is
  authoritative *except* for the platform administrator. That exception is the
  only thing standing between a remote group change and an operator losing
  access to their own instance, so it is stated as its own requirement rather
  than as a footnote to FR-005.
