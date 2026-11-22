# CTEM ticketing sync

A CTEM case lives in the organization's own case management system (Jira, ServiceNow, any provider that implements the contract below) without leaving XTM One. Code: `backend/app/services/ctem_ticketing.py`. Cases: `dev-docs/ctem-cases.md`.

## 1. Settings

The system is the ticketing target of the organization context (`OrganizationProfile.ticketing`): `integration` (the integration id), `project` (Jira project key, or the ServiceNow assignment group), and `sync` (`TicketingSync`), read at every sync so a change applies to the next one with no restart.

| Field | Default | Meaning |
|---|---|---|
| `create_on` | `mobilize_p1_p2` | A case gets its ticket when it passes Mobilize and is P1 or P2; `mobilize_p1`; `manual` (by hand per case). |
| `record_type` | Jira `Task`, ServiceNow `incident` | The Jira issue type, or the ServiceNow table: `incident`, `change_request` or `problem` (`SERVICENOW_TABLES`); the settings save answers 422 for another table, since each table numbers its states its own way. |
| `labels` | `[]` | Labels (Jira) or the category (ServiceNow, first label); `xtm-ctem` is always added. |
| `priority_map` | Jira P1 Highest, P2 High, P3 Medium; ServiceNow urgency 1, 2, 3 | Case severity to the system's priority value. |
| `status_map` | `{}` | The system's status name to a case ticket status, read before the default categories. |
| `done_starts_retest` | `true` | A ticket moved to done asks the case's re-test; the case closes on the re-test, never on the ticket. |
| `on_reopen` | `reopen` | A case reopened by a failed re-test reopens its ticket, or (`linked`) opens a new ticket that names the old one. |
| `due_date_from_sla` | `true` | The case SLA date becomes the ticket due date. |

The settings live on Settings > Organization > Identity and risk, card "Ticketing and intelligence", group "CTEM case sync" (`OrganizationTicketingSync.tsx`): read tiles at rest (an empty field names the backend default it falls back to), the fields above in edit mode, saved with the profile (`PUT /organization-context`, never on open). The form keeps `sync` whole through a save; a stored profile without it shows the defaults and saves none. A case without ticketing links to `/settings/organization?card=ticketing` (`ctem_ticketing.SETTINGS_PATH`).
## 2. Contract

`TicketingAdapter`: `find(correlation)`, `create(draft)`, `update(key, draft, status)`, `comment(key, text)`, `read_status(key)`, each raising `TicketingError(message, fix)`. A new provider adds one class to `ADAPTERS` (keyed by the integration's provider) built on its provider's HTTP helpers, and a row here.

| Provider | Correlation | Status read | Inbound |
|---|---|---|---|
| Jira | label `xtm-ctem-<16 hex>` | `statusCategory` (new, indeterminate, done) | a verified Jira webhook delivery (`jira_webhooks.dispatch_delivery`) moves the case at once (`ctem_ticketing.apply_jira_delivery`); the scheduled pull is the safety net |
| ServiceNow | `correlation_id` | `state`, per table (`_SERVICENOW_STATES` read, `_SERVICENOW_STATE_OF` written): incident 1 New, 2 In Progress, 3 On Hold, 6 Resolved, 7 Closed, 8 Canceled (writes 1, 2, 6, 8); change_request -5 New, -4 to 0 in progress, 3 Closed, 4 Canceled (writes -5, -1, 3, 4); problem 101 New, 102 to 104 in progress, 106 Resolved, 107 Closed (writes 101, 104, 106, 107) | polled by the case sweep (no outbound webhook needed) |

## 3. Case side

- One ticket per case by default, stored in `case.tickets` through `ctem_case_loop.record_ticket`; a split case may hold several.
- Idempotent: `ensure_ticket` reads the managed ticket on the case, then the system by correlation, and creates only when both are empty. A replayed `ctem_case.opened`, a retried run or a lost case write never creates a second ticket.
- Each managed ticket carries `sync`: `state` (`linked`, `in_sync`, `out_of_date`, `error`), `since`, `provider`, `correlation`, `external_status`, and for the last two states `error` and `fix` (the one action that fixes it), and `fix_code`, the stable code of that fix (`ctem_ticketing.FIX_CODES`): the UI words the fix in the reader's language from the code and never draws the English `error` or `fix` text, which stay for agents and logs. A read that fails keeps the `since` of the running out-of-date.
- A resolved case moves its ticket to done, an accepted risk to cancelled, a reopened case reopens it (or opens a linked one).
- No ticketing declared: `adapter_for` answers `No ticketing connected` and the case links to the settings; never an error.
- Tests: the rules on fakes in `tests/test_ctem_ticketing.py`; on PostgreSQL, `tests/test_ctem_ticketing_postgres.py` proves the pass opens nothing, one ticket per case over replayed calls and passes and a lost case write, the re-test asked once on a done ticket, and a pass that touches the declared organization only.

## 4. Schedule and events

The autonomous tick runs `ctem_ticketing.sync_cases` once per `CTEM_TICKETING_SYNC_SECONDS` (default 300, minimum 60, Redis slot `ctem:ticketing:sync`), after the case sweep. Per case of the primary organization (newest 200): the pass never opens a ticket (section 5); a case whose state asks the ticket to close (resolved: done, accepted risk: cancelled) or to reopen pushes it; every other managed ticket is pulled. Reopen is decided by `needs_reopen`: the sync closed the ticket for the case (`closed_by_case`), or a failed re-test is newer than the ticket's done; a ticket done while the case waits for its re-test is left done.

A pull that moves a ticket to done on an open case raises `ctem_case.retest_requested` (a viewer-checked case event, #933: the case id and routing with `ticket_integration` and `ticket_key`, heard only by readers of the case) once; the fleets run the re-test and the case closes on its result.

On screen (CTEM-19-UI, #1032): every sync step that creates, moves or fails on a case's ticket (and every inbound status that moves it) raises the `ctem_case_update` frame with `event_kind` `ticket`, so the ticket chip on the Cases board (Mobilization) and the case page moves without a reload; a re-test asked by a done ticket also writes a `retest_requested` row on the case history. The list and case reads answer `ticketing` (`connected`, `reason`, `settings`) from the profile alone, so the case page says `No ticketing connected` with the settings link. The chip is `frontend/src/pages/xtm-one/ctem/case-ticket.tsx`.

Inbound status (`apply_inbound_status`): a status an event carries for a ticket of the declared integration is mapped like a pull (customer `status_map` first) onto the case that manages that key, in the same transaction; a ticket no case manages, or an event of another integration, changes nothing. A delivery that fails to apply is logged and left to the next pull, never refused.

## 5. Gates

Opening a case's ticket is the gate `ctem.mobilize` of the CTEM gate catalog (#916, `dev-docs/ctem-fleets.md` section 11): the tool rule `sync_ctem_case_ticket` with `create: true` sits beside the free-form ticketing tools under that one key, so one Mobilize decision per case covers it, and with the fleet on autonomous it runs with no decision. A sync without `create` (push the case state, pull the ticket status) changes nothing the case has not already decided and is not gated; it answers whether `create_on` asks a ticket for the case, which the role then opens with `create`. The scheduled pass and the inbound events never open a ticket, so no ticket exists without the case's Mobilize decision. Segregation: the ticket carries only the case's own fields (title, fix, severity, trigger path, SLA date, the link back and the correlation), never an issue, a finding or a source the case's readers may not read, and it is written with the declared ticketing integration only.

## 6. Agents

The Mobilization role works the ticket through this layer only, never through a free-form ticketing tool call. The tool `sync_ctem_case_ticket` (`services/builtin_tools/ctem_cases.py`, on the CTEM case agents) calls `ctem_ticketing.sync_one` for one case. `sync_one` takes the same single step as the scheduled pass (`sync_case`): create when asked, push or pull otherwise. It answers the ticket with its sync state, or `connected: false` with the reason and the settings link when no ticketing is declared. The persona `ctem-remediation-coordinator.md` says so, and the two loop orchestrators' "Case opened or reopened" assignment also hears `ctem_case.retest_requested`: it runs the case's re-test, and the case closes on that result, never on the ticket.
