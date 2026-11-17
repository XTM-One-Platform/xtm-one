# Feature Specification: Keep the OpenCTI filter-key hint working across OpenCTI versions

**Feature Branch**: `030-opencti-filter-keys-error-compat`

**Created**: 2026-10-19

**Status**: Draft

**Input**: Upstream: [OpenCTI#18965](https://github.com/OpenCTI-Platform/opencti/issues/18965), fix in OpenCTI PR #19096.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The agent still gets a filter-key hint on newer OpenCTI (Priority: P1)

An agent lists entities with a filter key OpenCTI does not know (e.g. `labels` instead of `objectLabel`). Today OpenCTI rejects it with a code XTM One recognises, and the agent is told which valid keys are close so it can retry. Newer OpenCTI versions reject the same mistake with a different code and an HTTP-style status of 400 inside the error details (the GraphQL response itself stays HTTP 200). The hint must still appear, or the agent sees only the bare rejection and cannot self-correct.

**Why this priority**: the defect itself; without it the hint silently disappears on upgraded OpenCTI instances.

**Independent Test**: feed the list tool a rejection carrying the new code and a 400 status, naming the key `labels`, and check the result carries a hint that points to `objectLabel`.

**Acceptance Scenarios**:

1. **Given** OpenCTI rejects an unknown filter key with the new code, **When** the agent's call fails, **Then** the result keeps OpenCTI's error and adds the same close-key hint as today.
2. **Given** the new code with a 400 status in the error details, **When** the call fails, **Then** the status value has no effect on whether the hint is added.

---

### User Story 2 - Older OpenCTI versions keep working (Priority: P1)

Deployments connected to an OpenCTI that has not been upgraded keep rejecting unknown filter keys with the old code and a 500 status. The hint must keep appearing for them.

**Why this priority**: both versions are in use during the rollout; fixing one must not break the other.

**Independent Test**: feed the list tool a rejection carrying the old code and a 500 status and check the hint is unchanged.

**Acceptance Scenarios**:

1. **Given** OpenCTI rejects an unknown filter key with the old code, **When** the call fails, **Then** the hint is added exactly as before.

---

### Edge Cases

- A rejection with either code but no rejected keys named: returned untouched, no schema lookup (unchanged).
- A rejection with another error code, or an unrelated message: no hint (unchanged).
- The key schema cannot be fetched, is empty or is too slow: the original error is returned (unchanged).
- Upstream rewords the rejection message: the hint is skipped rather than guessed; see Assumptions.
- The same code also covers a key with invalid characters (second message, same `filter keys` wording): the hint behaves as it does for it today.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST add the close-key hint to a rejected-filter-key error whether OpenCTI reports it with the old code or the new code.
- **FR-002**: The hint MUST NOT depend on the status value in the error details (500 or 400).
- **FR-003**: Errors with neither code MUST be returned unchanged, as today.
- **FR-004**: Every existing hint behaviour (no keys named, empty or unreachable schema, slow lookup) MUST be unchanged for both codes.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For an unknown filter key, the agent receives the close-key hint in 100% of cases, on both the old and the new OpenCTI rejection.
- **SC-002**: The existing hint test cases pass unchanged for both rejection shapes.

## Assumptions

- The new code is `BAD_USER_INPUT_ERROR` with a 400 status, confirmed in OpenCTI PR #19096 (open, not merged; it waits on this change). The message and the `keys` attribute are unchanged there.
- The name changed once during upstream review (from `FILTER_KEYS_ERROR`); re-check it before release. A reworded message would also skip the hint and need a follow-up.
- OpenCTI's log level for this rejection (error → info) does not affect XTM One.
