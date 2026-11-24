# CTEM data segregation audit (13 Nov 2026)

Question: can a user of XTM One see, through any CTEM surface, data that their
own permissions in OpenCTI (groups, roles, markings, organization
segregation), OpenAEV or OpenCRQ hide from them? A count, a score or a name is
data. XTM One itself is not multi-tenant: out of scope, except that XTM One
must never cross an OpenAEV / OpenCRQ tenant boundary when it calls them.

Read on `main` 42250c43 and the fix branch of #11613. Verdicts: **segregated**,
**leak** (with severity), **not verifiable** (from XTM One's code alone).

## Summary

| # | Surface | Verdict | Fix |
|---|---|---|---|
| 1 | Live reads of OpenCTI / OpenAEV / OpenCRQ (posture, stages, cases source, agent tools, MCP) | segregated | - |
| 2a | CTEM Cases (`ctem_cases*`), API and agent tools | **leak, high** | #11613 (this PR) |
| 2b | Posture cache (Redis) and `CtemPostureSnapshot` | segregated | - |
| 2c | Exposure briefing and stage assessments (stored runs) | segregated | - |
| 2d | Command center layouts | segregated (no data) | - |
| 2e | `ctem_metrics` table | not used (no reader, no writer) | - |
| 3a | CTEM fleets F1, F2, F4 (company-managed runs and `ctem:f*:state`) | **leak, medium** (opt-in, off by default) | decision owed, see 3a |
| 3b | Unattended module calls (service-account rule) | segregated | - |
| 4 | Writes back to OpenCTI / OpenAEV / OpenCRQ | segregated (per-user JWT) | - |
| 5 | OpenAEV / OpenCRQ tenant boundary | segregated (read on the target side, 21 Nov 2026) | - |
| 6 | Admin-only surfaces | segregated after #11613; organization profile shared by design | - |
| 7a | CTEM signals (API, agent tools, activity stream) | segregated for users; administrators read all (decision owed) | - |
| 7b | Scope gate (`ctem_scope_gate_store`) | **leak, medium**: fixed | #933 |
| 7c | CTEM case platform events (`ctem_case.*`) | **leak, high**: fixed | #933 |

Refreshed 21 Nov 2026 after the CTEM loop audit (signals, scope gate, case
loop): rows 2a, 5, 6 updated, rows 7a to 7c added.

## 1. Live reads

Every CTEM read resolves credentials through
`ctem_metrics_collector._user_creds` (`backend/app/services/ctem_metrics_collector.py`,
`_user_creds`), which asks `integration_tools._get_token(..., user_id=...)`.
For a `platform_managed` (registered) integration `_get_token` mints a short
EdDSA JWT for the acting user, addressed to the registered platform, and
returns nothing without a user (`integration_tools.py`, the `platform_managed`
branch: "no user context available - cannot generate JWT"). The integration
row itself is filtered by `integration_visible_filter(db, user_id)`, so an
access-restricted row serves its grantees only.

- Posture, stages, sync and the cases source (`ctem_cases_source.read_open_issues`)
  all go through `_user_creds`.
- Agent integration tools and the MCP platform server
  (`api/v1/mcp_platform.py`, `_build_platform_tools`, `context.user_id =
  user.id`) use the same per-user JWT.
- Unregistered deployment: the company-managed static key serves the posture
  "as it always did" (`_user_creds` docstring; Sam's rule 1 of 13 Nov 2026,
  `tests/test_unattended_module_identity.py::test_an_unregistered_deployment_keeps_its_static_key`).
  On such a deployment every user reads with one key: the source product
  cannot segregate what it cannot tell apart. Accepted by that rule; a
  registered deployment never uses it for CTEM modules.

Verdict: segregated on a registered deployment.

## 2. Stored and cached data

### 2a. CTEM Cases: leak, high (fixed by #11613)

Before: `ctem_cases`, `ctem_case_issues`, `ctem_case_events` were keyed by
`organization_id` and `vertical`. `POST /recompute` (admin-only) built them
from the admin's JWT; `GET /ctem-cases`, `GET /{id}`, `PATCH /{id}` and the
tools `list/get/create/update_ctem_case` needed only a signed-in user. Any
user read CVEs, issue names, assets, crown jewels, CVSS / EPSS / KEV, case
counts by state and history of the admin's view, and could change a case's
state and owner. Live since #11592, #11605, #11609 (13 Nov 2026).

After (Sam's design, 13 Nov 2026: shared cases, per-viewer content, the
source products are the only judge):
- The database stores ids and workflow only (migration `ctemscope11613`
  drops every content column and deletes the rows built from one view).
  No longer true since the case loop (21 Nov 2026): a case built from
  signals stores a title and a fix key from the signal's subject, a severity,
  an SLA, risk scenario ids, tickets and validation references, the same for
  every viewer of the case. Presence stays per viewer (absent and 404 for a
  viewer who sees no member). Per-viewer rendering of those fields is a
  decision owed (shared organization data, or masked per viewer).
- `services/ctem_cases_view.py` renders every case for the viewer from the
  viewer's own source read (their JWT), cached per user for 5 minutes: a case
  with no visible issue is absent from the list and the counts and a 404; an
  issue they cannot see is a bare `{"restricted": true}` row; priority,
  scores, assets and crown jewels are computed on the visible issues; a
  history line about a hidden issue says "Restricted".
- Recompute merges (`ctem_cases.plan_recompute`): it adds and reopens what the
  caller sees open and closes a member only when the caller's read confirms
  it closed by id; a narrower view never closes or shrinks a case.
- API and the four agent tools go through the same view; an owner must see
  the case (checked with the owner's own read); an issue the actor cannot see
  cannot be split out.

Tests:
`tests/test_ctem_cases_postgres.py::test_two_users_with_different_visibility_share_cases_and_see_only_their_issues`
(PostgreSQL: Alice sees CVE-1 and CVE-2, Bob CVE-1, Carol neither; one shared
case; Bob's recompute leaves it whole; Bob gets CVE-2 as a bare Restricted
row and scores on CVE-1 only; Carol gets no case and no count; no content
column exists),
`tests/test_ctem_cases_api.py::test_a_viewer_sees_the_shared_case_with_only_their_issues_and_restricted_rows`,
`::test_a_case_with_no_visible_issue_is_absent_and_404`,
`::test_an_owner_who_sees_none_of_the_issues_is_refused`,
`tests/test_ctem_case_tools.py::test_an_agent_of_a_user_who_sees_no_issue_neither_lists_reads_nor_changes_the_case`,
`tests/test_ctem_cases.py::test_a_narrower_view_never_closes_or_shrinks_a_case`,
`tests/test_ctem_cases_source.py::test_recompute_merges_and_resolves_only_what_the_reader_confirms_closed`.

Staleness: a permission removed at source shows for up to 5 minutes (the
per-user view cache). The Cases UI (S4) owes the Restricted row rendering and
its UX round; no frontend renders Cases yet.

### 2b. Posture cache and snapshots: segregated

Redis keys `ctem:posture:{user_id}:{vertical}` and `ctem:facts:{user_id}:{vertical}`
(`api/v1/ctem_metrics.py`); `CtemPostureSnapshot` carries `user_id` and every
read filters on `CtemPostureSnapshot.user_id == user.id`. The funnel and stage
visuals are computed from the same per-user posture.

### 2c. Briefing and stage assessments: segregated

`GET /ctem/briefing` and `GET /ctem/stages/{stage}/assessment` read
`AssignmentRun` rows with `AssignmentRun.user_id == user.id`
(`_latest_vertical_artifact`). The exposure briefing and the five stage
analysts run per user (`run_as_all_users`, `hide_runs` true,
`agents/builtin_agents.py`), so their runs are born `is_company_managed=False`
(`assignment_helpers.resolve_hide_runs`, `assignment_engine.py`) and stay out
of every other user's run lists (`helpers.run_visible_filter`).

### 2d. Layouts: segregated

`users.ctem_dashboard_layouts` holds widget ids and places only
(`services/dashboard_layouts.py`), per user.

### 2e. `ctem_metrics`: unused

The model `CtemMetric` has no reader or writer in `backend/app`.

## 3. Agents

### 3a. CTEM fleets F1, F2, F4: leak, medium (decision owed)

The orchestrators `ctem-threat-response-orchestrator` (F1),
`ctem-vuln-prioritization-orchestrator` (F2) and `ctem-posture-orchestrator`
(F4) are company-managed, seeded under an administrator, with no `hide_runs`
and `run_as_all_users` false (`agents/builtin_agents.py`, `_F1_RUN`,
`_CTEM_F2_COMMON`, `CTEM_POSTURE_ASSIGNMENTS`). Their runs therefore execute
as the seed owner (`assignment_engine._execution_identity`: `assignment.user_id`),
read OpenCTI / OpenAEV / OpenCRQ with that administrator's JWT, and are
stamped `is_company_managed=True`. Two outputs reach every user:
- the runs (results, tool outputs): `helpers.run_visible_filter` shows
  company-managed runs to all users, read-only;
- the fleet blackboards `ctem:f1:state`, `ctem:f2:state`, `ctem:f4:state`
  (ranked CVEs, threat cases, board report): company-managed unfiled shared
  state is readable by every user (`services/shared_state._visible_clause`).

A user whose OpenCTI markings or OpenAEV scope hide a vulnerability or an
asset can read it there. Severity medium: the fleets are off until an
administrator opts in (`SEEDED_FLEET_OPT_IN = {"enabled": False,
"admin_opt_in": True}`), and the board report is meant for the organization.
The fix is a product decision, not a filter: (A) the fleet's runs and state
private to administrators (hidden runs, state filed in an admin-only
workspace), (B) keep them organization-wide and say so on the opt-in card
("everything these fleets read with the administrator's access is visible to
every user"), or (C) per-user fleets. Recommended: A.

### 3b. Unattended module identity: segregated

`services/module_identity.py` (`MODULE_PROVIDERS` = opencti, openaev,
opencrq): on a registered deployment, a CTEM or organization-context call in
an unattended run is refused on a credential that is not the acting person's
own (company API key, ownerless key, someone else's connection);
`tests/test_unattended_module_identity.py::test_a_module_is_called_as_the_acting_person_only`.

## 4. Writes back to the source products: segregated

Writes go through the same integration tools and `_get_token` per-user JWT:
the source product applies the acting user's own rights. The fleets of 3a
write as their seed administrator by the same rule, which is the
administrator's explicit opt-in.

## 5. OpenAEV / OpenCRQ tenants: segregated

XTM One mints one JWT per user addressed to the registered platform URL
(`platform_token_audience`) and never names a tenant in the call. Read on the
target side (21 Nov 2026): OpenAEV resolves the XTM One JWT to a user by
email and applies that user's rights on every route
(`security/token/XtmJwksExtractor`); OpenCRQ resolves it by subject under
its tenant isolation (`back/src/trpc.ts`). The tenant check after the user
is resolved is inferred from that code, not exercised in a lab.

## 6. Admin-only surfaces

- `POST /ctem-cases/recompute`: no longer admin-only; it merges only what the
  caller sees and closes only what the caller's read confirms closed (#11613).
- `GET/PUT /ctem/stage-agents`: `require_admin`; agent assignment only, no
  source data.
- Organization context writes (#11594): administrators' agents only. The
  profile imports from OpenCTI, OpenAEV and OpenCRQ with the administrator's
  access; source segregation and markings are not kept and every user and
  agent of the organization reads it. By design: the profile is organization
  data an administrator publishes, and its import tools say so.

## 7. The CTEM loop: signals, scope gate, case events (21 Nov 2026)

### 7a. Signals: segregated for users

`GET /ctem-signals`, `GET /{id}`, the agent tools and the counts answer the
caller's own signals; an administrator reads every signal of the
organization, including the `system` signals of the integration listeners
(OpenCTI object name and a bounded trigger message). The activity stream sends
ids only to the recorder and the administrators. Whether administrators may
read listener copies without their OpenCTI rights being checked is a decision
owed (organization data, or ids only).

### 7b. Scope gate: leak, medium (fixed by #933)

Before: `POST /ctem-signals/evaluate` and `/{id}/evaluate` read OpenCRQ risk
scenarios with the administrator's connection and wrote their ids, names and
criticality into every gated signal, including signals another user recorded,
who read them back; `related_signals` listed ids and verdicts of other
recorders' signals.

After: a signal is decided with its recorder's rights.
`ctem_scope_gate_store.scenario_reader` names the recorder's own OpenCRQ
connection (a `system` signal, read by administrators only: the evaluating
administrator's); the bulk pass reads once per recorder. `related_signals`
applies the signal API's rule: an administrator and a `system` signal see
every signal, anyone else only their own. A signal decided before the fix
keeps its stored decision until its next evaluation.

Tests: `tests/test_ctem_segregation_events_gate.py::test_an_admin_evaluation_writes_no_admin_scenario_into_a_user_signal`,
`::test_related_signals_of_a_non_admin_recorder_are_their_own`.

### 7c. Case events: leak, high (fixed by #933)

Before: the four `ctem_case.*` events were platform-wide: any user's
`platform_event` assignment, flow or task watch was woken for every case with
severity, SLA, owner, risk scenario ids, the signal id and the validation
entry with its note.

After: they are viewer-checked (`platform_events.VIEWER_CHECKED_EVENTS`).
Each delivery checks with `ctem_cases_view.user_sees_case` (the API's rule)
that the owner and the user the run executes as see the case; an
administrator's `scope: all` subscription running as itself hears every case,
never through another run-as user. An unreadable view refuses the delivery.
The payload is `case_id`, `vertical`, `state`, `trigger_path`; the run reads
the rest with `get_ctem_case` under its own view.

Tests: `tests/test_ctem_segregation_events_gate.py` (non-administrator not
woken, run-as user checked, administrator scope all, unreadable view, flows).
