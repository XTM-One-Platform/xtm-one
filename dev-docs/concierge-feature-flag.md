# Platform Concierge - Feature Flag

The Platform Concierge (#1783, PRD in `platform-concierge.md`) ships behind the
`CONCIERGE` feature flag (#8254), off by default, while its levels land. The
flag is **enforced by the backend**: with the flag off the Concierge is dark,
not merely hidden. It has no deployment condition: both editions, every mode
(decision B20).

## Enabling it

```
ENABLED_FEATURES=CONCIERGE
```

The flag is read once at startup: set it on the platform and on the worker
(the fleet's assignment runs act as the Concierge identity there), and restart
them. `ENABLED_FEATURES=*` enables it too.

The flag only makes the Concierge available. It starts work once an
administrator turns it on in Settings > Concierge (`concierge_settings.enabled`,
`services/concierge/availability.concierge_active`).

## What the flag gates

| Surface | With the flag off |
| ------- | ----------------- |
| The Concierge identity | No run acts as it (`identity.concierge_principal_for` and `current_concierge` answer `None`), so every Concierge tool refuses, including in a run already in flight at its next check |
| Consent reads | Nothing is readable (`consent.load_consent_scope` is inactive, `consent_filter` is `false`, `content_readable` is `False`) |
| Budget | `budget.budget_refusal` answers `concierge_disabled`; no cycle opens |
| `/api/v1/concierge/*` | Every route answers 403 naming the flag (router-level `require_feature`) |
| Agent page | No Concierge improvement opt-in card |
| Profile | No Concierge opt-out card |
| Settings | No Concierge tab |
| Fleet seeding (#8261) | `fleet_seed.seed_concierge_fleet` creates nothing; agents seeded while it was on are left as they are (no run acts as the Concierge) |
| Scheduler (#8261) | `cycles.concierge_tick` starts no cycle and evaluates no signal; cycles already open still close and get their digest |
| `concierge_orchestrator` tools (#8261) | Not wired in chat, runs or the platform MCP server; the group is withheld from the tool catalog |
| Every `gate="concierge"` built-in group (sandbox, evaluation, improvement proposals, memory steward, diagnosis, experiments, Flow Lab, orchestrator) | Withheld from the offered tool catalog, the prompt guard's scan list and the tool approvals list (`builtin_tool_catalog.withhold_concierge_tools`); not wired in chat or runs; `list_improvements` refuses |
| `/api/v1/improvement-proposals` | Served and published only while `EVALUATION` or `CONCIERGE` is on (403 otherwise) |
| The seeded fleet (`concierge_role`) | Hidden from every listing, picker and `/v1/models`, administrators included, and refused by id (`helpers.hidden_rows_excluded`) |
| Concierge page, agent and flow Improvements (#8261) | Hidden (`FeatureFlagGate`, `useFeatureFlag`) |

## What is kept

- `platform_settings.concierge_settings` (enable, scope, budget caps).
- The opt-ins (`agents.concierge_improvement_opt_in`,
  `flows.concierge_improvement_opt_in`) and the opt-outs
  (`users.concierge_opt_out`): turning the flag back on reads them as they were.
- `agents.concierge_role` on the fleet's agents (listed again once the flag is back on).
- The budget ledger (`concierge_cycles`, `concierge_budget_months`, `concierge_budget_work`).
- Cycle digests, signal states and schedule marks (`concierge_cycle_runs`,
  `concierge_signal_states`, `concierge_schedule_marks`, #8261).

## Removing the flag

Once the Concierge ships, drop `CONCIERGE` from `KNOWN_FEATURE_FLAGS` /
`FeatureFlag` (`backend/app/core/feature_flags.py`) and from `KNOWN_FEATURE_FLAGS` in `frontend/src/lib/utils.ts`, make
`services/concierge/availability.concierge_enabled` answer `True`, and remove
the gates listed above.

`backend/tests/test_concierge_identity_unit.py` pins the flag and the identity; `test_concierge_routes_postgres.py` pins the routes.
