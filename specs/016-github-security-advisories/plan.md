# GitHub Repository Security Advisories Implementation Plan

**Branch**: `feat/3618-github-security-advisories`  
**Date**: 2026-09-17

## Summary

Add native, read-only access to repository security advisories and expose the
`repository_advisory` webhook event to Assignments. Keep the existing global
`security_advisory` event compatible but label it explicitly. Reuse the current
GitHub integration, generic webhook router, and Assignment execution model.

## Scope

- Add `list_github_repository_security_advisories`.
- Add `get_github_repository_security_advisory`.
- Add `repository_advisory` to the GitHub event catalog.
- Relabel `security_advisory` as a global advisory event without changing its
  event key.
- Add the required GitHub App permission and token guidance to the setup guide.
- Add focused tool, catalog, and webhook-routing tests.
- Update affected GitHub integration documentation.

## Non-Goals

- Creating, editing, publishing, closing, or requesting a CVE for an advisory.
- Organization-wide advisory listing, which requires broader authority and
  write-level advisory permission.
- Automatically updating existing managed GitHub webhook subscriptions.
- Adding an advisory scheduler or persistence model. Periodic reconciliation is
  configured as a scheduled Assignment.
- Modernizing the legacy GitHub MCP preset.
- General repair of the repository-webhook event picker taxonomy.

## Technical Context

**Runtime**: Python 3.14, async FastAPI backend  
**HTTP**: `httpx` through `app.integrations.providers.github._common.safe_request`  
**Tool API**: `CallableToolExecutor` and `ToolDefinition`  
**Testing**: pytest with mocked GitHub HTTP responses  
**Storage**: N/A; no schema or migration changes  
**Frontend**: No change expected; existing views render the backend event catalog

## Constitution Check

- **Assignment-driven automation**: Pass. Webhooks and schedules trigger
  Assignments; no new background execution path is introduced.
- **Fail closed**: Pass. Invalid repositories and advisory identifiers are
  rejected before an HTTP request; GitHub authorization remains credential-bound.
- **Security by default**: Pass. The feature is read-only and requests the
  narrow repository advisory permission.
- **Shared utilities**: Pass. Use `parse_repo`, `safe_request`, and
  `with_permission_hint`; do not duplicate GitHub HTTP or validation logic.
- **CI quality gates**: Pass when focused pytest and Ruff checks succeed.
- **Minimal writing**: Pass. Decisions appear once in this plan; implementation
  detail belongs in code and tests.

## Design

### Repository Advisory List

Implement in `backend/app/integrations/providers/github/security.py` using:

```text
GET /repos/{owner}/{repo}/security-advisories
```

Inputs:

- `repo`: required `owner/name`, validated by `parse_repo`.
- `state`: optional `triage`, `draft`, `published`, or `closed`.
- `sort`: `created`, `updated`, or `published`; default `updated`.
- `direction`: `asc` or `desc`; default `desc`.
- `count`: default 20, clamped to 1-30.
- `after`: optional cursor returned by GitHub pagination.

The compact result includes GHSA ID, state, severity, CVE, summary, affected
packages and version ranges, update time, and `html_url`. It omits full
descriptions, collaborators, and private-fork data. If the response has a next
cursor, include a continuation hint the agent can pass back as `after`.

### Repository Advisory Detail

Implement in the same module using:

```text
GET /repos/{owner}/{repo}/security-advisories/{ghsa_id}
```

Validate `ghsa_id` as a canonical, case-insensitive `GHSA-xxxx-xxxx-xxxx`
identifier before interpolation. Return summary, bounded description, state,
severity, CVE and GHSA identifiers, CVSS v3/v4, CWEs, affected packages and
versions, credits, lifecycle timestamps, and `html_url`. Bound individual text
fields and total output so an advisory cannot consume the agent context.

### Permission Errors

On authorization errors, append one actionable hint through
`_common.with_permission_hint`:

- GitHub App or fine-grained PAT: grant `Repository security advisories: Read`
  and repository access.
- Classic PAT or OAuth token: grant `repo` or
  `repository_advisories:read`, with the acting user authorized for the
  advisory.

Do not rewrite ordinary 404 responses because GitHub may intentionally conceal
private resources as not found.

### Webhook Catalog

In `backend/app/api/v1/integration_catalog.py`:

- Keep event key `security_advisory`; label it `Global security advisory` and
  describe actions `published`, `updated`, and `withdrawn`.
- Add event key `repository_advisory`; label it `Repository security advisory`
  and describe documented actions `published` and `reported`.
- Add `Repository - Security advisories: Read` to the GitHub App setup guide.
- State that webhook delivery and API reads both depend on the App permission,
  installation repository selection, and acting user's access.

No webhook receiver change is planned. The existing receiver derives
`repository_advisory.<action>` from `X-GitHub-Event`, and existing prefix
matching lets an Assignment subscribe to `repository_advisory`.

## Implementation Steps

1. Extend the GitHub tool-surface test with both advisory tool names.
2. Add failing list/detail HTTP-contract and validation tests.
3. Implement list formatting, detail formatting, bounds, and permission hints.
4. Run the focused GitHub integration test before further edits.
5. Add and distinguish the two advisory events in the provider catalog.
6. Add setup-guide permission and authentication guidance.
7. Add catalog assertions and webhook prefix-routing tests for `published` and
   `reported`.
8. Update the GitHub provider summary in
   `backend/app/integrations/providers/github/__init__.py` and
   `backend/app/integrations/AGENTS.md`.
9. Check `docs/docs/` and update GitHub integration guidance if an applicable
   page exists.
10. Run focused tests, Ruff, and the broader required backend checks.

## Test Matrix

### Tool Surface and Validation

- Both tools are defined and registered exactly once.
- Invalid repository values make no HTTP request.
- Invalid or placeholder GHSA IDs make no HTTP request.
- `count` is clamped and blank optional filters are omitted.
- Unsupported state, sort, or direction values are rejected locally.

### GitHub HTTP Contract

- List and detail use the exact repository advisory endpoints.
- Requests carry the shared GitHub headers and pinned API version.
- List passes state, sort, direction, count, and cursor correctly.
- Empty lists return a clear no-results response.
- Null-heavy and multi-package advisories format without failure.
- A next `Link` cursor becomes a usable continuation hint.
- 401/403 errors receive the permission hint; 404 remains unchanged.

### Output Safety

- Long descriptions are truncated with an explicit marker.
- Multiple vulnerabilities and credits are bounded.
- Collaborator and private-fork fields are not rendered.

### Event Routing

- Catalog contains distinct `security_advisory` and `repository_advisory`
  entries.
- Catalog setup includes repository advisory read permission.
- `repository_advisory.published` matches an Assignment configured for
  `repository_advisory`.
- `repository_advisory.reported` matches the same prefix.
- Existing global advisory configuration remains unchanged.

## Validation

Run after the first tool implementation edit:

```bash
cd backend
pytest tests/test_github_integration.py -q
```

Run after catalog and routing changes:

```bash
cd backend
pytest tests/test_github_integration.py tests/test_provider_webhook_routing_cache.py -q
ruff check app/integrations/providers/github/security.py \
  app/integrations/providers/github/__init__.py \
  app/api/v1/integration_catalog.py \
  tests/test_github_integration.py \
  tests/test_provider_webhook_routing_cache.py
```

Before submission, run the repository-required backend checks relevant to the
touched surface and `git diff --check`.

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Private advisory disclosure | GitHub credential authorization remains the boundary; request read-only permission and omit sensitive list fields. |
| Misleading webhook coverage | Document only `published` and `reported`; use a scheduled Assignment for reconciliation. |
| Excessive agent context | Clamp list size and bound detail fields and collections. |
| Existing hook misses new event | State that existing managed hooks must be updated or recreated; defer reconciliation. |
| GitHub hides authorization as 404 | Preserve 404 rather than asserting whether the resource exists. |
| Existing global event breaks | Keep the `security_advisory` key unchanged and alter only its display label. |

## Deferred Follow-Ups

1. Add organization-wide advisory listing only with an explicit write-permission
   security review.
2. Add advisory mutations as separately gated write tools.
3. Add managed webhook update and reconciliation support.
4. Add webhook target metadata so repository hook pickers exclude App-only and
   organization-only events.
5. Replace the deprecated GitHub MCP preset with the official
   `github/github-mcp-server` configuration.

## Done When

- Agents can list and inspect repository security advisories through native
  GitHub integration tools.
- Users can select the repository advisory event and configure a matching
  Assignment.
- Setup guidance states the exact least-privilege access required.
- Global advisory event compatibility is preserved.
- Focused tests and Ruff checks pass.
- No migration, frontend state path, or ad-hoc automation is introduced.

## Planning Prerequisite

The feature currently has no saved `spec.md` and no `.specify/feature.json`, so
the full SpecKit Phase 0/1 artifact workflow cannot run. Create or restore the
feature specification before generating `research.md`, `data-model.md`,
contracts, `quickstart.md`, or `tasks.md`.