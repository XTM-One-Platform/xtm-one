# Feature Specification: OpenAEV Tenant Scoping

**Feature Branch**: `fix/5335-openaev-tenant-write-scope`

**Created**: 2026-10-05

**Status**: Draft

**Input**: On a multi-tenant OpenAEV, agents cannot create scenarios, add injects or launch simulations (`TENANT_WRITE_SCOPE`).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Author and launch simulations on a multi-tenant OpenAEV (Priority: P1)

An agent connected to a multi-tenant OpenAEV creates a scenario, adds injects to it and launches a simulation. Today each write fails with `TENANT_WRITE_SCOPE`.

**Why this priority**: This is the reported defect; scenario authoring is unusable on multi-tenant instances without it.

**Independent Test**: Against a multi-tenant OpenAEV, run create-scenario, add-inject and launch-simulation; all succeed and land in the default tenant.

**Acceptance Scenarios**:

1. **Given** a multi-tenant OpenAEV that exposes a default tenant, **When** the agent creates a scenario, **Then** it is created in that tenant with no `TENANT_WRITE_SCOPE` error.
2. **Given** the same instance and an existing scenario, **When** the agent adds an inject and launches a simulation, **Then** both succeed in the default tenant.

---

### User Story 2 - Every OpenAEV tool works on a multi-tenant instance (Priority: P1)

Writes in the other tool modules (assets, teams, access, taxonomy, content, testing, injects, expectations, and the rest) hit the same rejection today. The fix covers all of them, so the next module does not become the next issue.

**Why this priority**: Fixing only scenarios and simulations leaves roughly 90% of the write surface broken.

**Independent Test**: For each tool module, one representative write succeeds on a multi-tenant instance.

**Acceptance Scenarios**:

1. **Given** a multi-tenant OpenAEV, **When** the agent invokes any OpenAEV tool, read or write, **Then** the request targets the default tenant.
2. **Given** a tool module added later, **When** it makes OpenAEV requests, **Then** it is tenant-scoped without per-module code.

---

### User Story 3 - Single-tenant instances are unchanged (Priority: P1)

An OpenAEV that exposes no default tenant behaves exactly as before.

**Why this priority**: The fix must not regress existing deployments.

**Independent Test**: Against a single-tenant OpenAEV, existing tool behaviour and requests are identical to before.

**Acceptance Scenarios**:

1. **Given** an OpenAEV whose settings report no default tenant, **When** any tool runs, **Then** it uses the same routes as today.

---

### User Story 4 - A failed tenant lookup is diagnosable (Priority: P2)

If the platform's settings cannot be read, tools keep working as today, and a tenant-scope rejection names the lookup as a possible cause.

**Why this priority**: Failing every tool would break instances whose token cannot read settings; staying silent hides why a multi-tenant write was rejected.

**Independent Test**: Make the settings read fail and a tool request be rejected for tenant scope; the error names the failed tenant lookup.

**Acceptance Scenarios**:

1. **Given** the settings read fails, **When** a tool runs and its request succeeds, **Then** the result is unchanged from today.
2. **Given** the settings read fails, **When** a tool's request is rejected for tenant scope (`TENANT_WRITE_SCOPE`, `TENANT_SELECTOR_REQUIRED`), **Then** the error also names the failed tenant lookup and its cause; other errors (e.g. 404) are unchanged.
3. **Given** the settings read succeeds with no default tenant, **When** any tool runs, **Then** it proceeds unscoped (User Story 3).

### Edge Cases

- Settings read fails, including an unparseable response: the failure is remembered as a failure, not as "no tenant", and is not retried within the tool set.
- A request path is already tenant-scoped: it is not scoped twice.
- Tools that call OpenAEV outside the shared request helper (direct HTTP calls) are scoped too.
- Token lacks permission to read settings: treated as a failed lookup (FR-005), not as "no tenant".
- Routes with no tenant-scoped twin (settings, platform users/roles/groups, schemas, the caller's own `/me/*` endpoints) and autonomous-run callbacks, whose tenant OpenAEV derives from the run itself, are never rewritten.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Every OpenAEV tool request, except those under FR-009 and FR-010, MUST target the default tenant's routes when the platform exposes a default tenant.
- **FR-002**: Reads MUST be scoped identically to writes.
- **FR-003**: The default tenant MUST be resolved at most once per tool set.
- **FR-004**: Tenant scoping MUST be applied in one shared place, not re-implemented per tool module.
- **FR-005**: The system MUST distinguish "no default tenant" from "tenant lookup failed".
- **FR-006**: On a failed lookup, a tool MUST proceed unscoped and, if its request is rejected for tenant scope, MUST name the failed lookup in the error.
- **FR-007**: With no default tenant, request routes MUST be unchanged from current behaviour.
- **FR-008**: A path that is already tenant-scoped MUST NOT be rewritten.
- **FR-009**: Tools that already scope themselves (Threat Arsenal) MUST keep their behaviour.
- **FR-010**: Routes with no tenant-scoped twin, and autonomous-run callbacks (the run decides its tenant), MUST NOT be rewritten.

### Key Entities

- **Default tenant**: the tenant the platform reports in its settings; the target of all scoped requests.
- **Tool set**: the OpenAEV tools bound for one integration instance; owns one tenant resolution.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On a multi-tenant instance, 0 OpenAEV tools return `TENANT_WRITE_SCOPE`.
- **SC-002**: The `factory-scenario-creator` flow (create scenario, add injects, launch) completes end to end.
- **SC-003**: Binding a tool set triggers at most 1 settings request.
- **SC-004**: Single-tenant requests are byte-identical to before the change.
- **SC-005**: 100% of tool modules are covered by a test that fails if a request escapes tenant scoping.

## Clarifications

### Session 2026-10-05

- Q: Fix only scenarios and simulations, or all modules? → A: All modules, via one shared mechanism; the issue's patch covers 2 of ~20 and was rejected as the design.
- Q: Scope reads or writes only? → A: Both; mixed scoping is harder to reason about and the issue reports reads working only through the tenant-scoped token.
- Q: What happens when the settings read fails? → A: Proceed unscoped and name the lookup in a tenant-scope rejection (FR-006); failing every tool would break single-tenant instances whose token cannot read settings.

## Assumptions

- Every tenant-owned route family the tools use has a tenant-scoped twin; the exceptions are FR-010 (audited in `research.md`).
- The default tenant is stable for the life of a tool set.
- Out of scope: selecting a non-default tenant per call.
