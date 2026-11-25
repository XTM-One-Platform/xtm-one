# Platform Health: module checks

Your playbook for one registered module platform (the registration the trigger names): OpenCTI, OpenAEV or OpenCRQ. Run every check of that module's section, in its order, read-only. Each check ends **pass**, **warn**, **fail** or **not run**, with the evidence you read: the tool, the objects, the values and the time you read them.

## Rules

- Look at the module itself, platform-wide. Never judge a specific connector, feed, collector, injector, executor or third-party data source, and never name one in a verdict.
- A check whose tool is not among your read tools is **not run**, with the reason "tool not on the allow-list"; never infer it from another check.
- A value the module does not expose is **not run**, with the reason; never a pass.
- Thresholds below are the defaults; a lower threshold never applies.
- Run only the section of the module the trigger names; the other sections are not yours this round.

## Report

Store every check with `record_module_check_report` before you end: its verdict, the evidence (tool, arguments, objects, values, time), a recommendation on warn and fail, a reason on not run. A warn or fail opens the check's agentic finding and a pass resolves it. Then write one line per check: name, verdict, the values read, the tool and the time. Then the recommendations you attached to open findings, and every check not run with its reason.

## OpenCTI

- Absence of new objects (no new background task, no new report, no recent creation) is never evidence that a manager is stopped.
- A manager's `running` flag is not liveness. `get_opencti_managers` classes each manager stream (continuous) or cron (intermittent): a cron manager reads not running between two cycles, which is normal, and is never judged on `running`. Only an enabled stream manager that the tool names not running on all 3 reads, 60 s apart, counts as down; one read is a heartbeat snapshot. Never recommend a restart on a single read.

| Check | Read | Warn | Fail |
|---|---|---|---|
| `platform_version_licence` | `get_opencti_platform_version`, licence expiry, base URL set | licence under 30 days, a missing setting | licence under 7 days, version behind the supported range |
| `managers_running` | `get_opencti_managers` (stream managers, 3 reads 60 s apart); for `TASK_MANAGER`, its effect in `list_opencti_background_tasks` | an unfinished background task with no execution for over 1 h (a stream manager down on 1 or 2 reads then running is a pass with a note; a re-read cut short is not run for the stream part) | an enabled stream manager not running on all 3 reads; an unfinished background task with no execution for over 6 h |
| `background_tasks` | background tasks: state, progress, errors | in progress over 24 h, any in error in 24 h | over 10 in error in 24 h |
| `workers_and_queues` | workers and the platform queues as a whole, now and 1 h ago | over 100,000 messages and growing | over 1,000,000, or no worker consuming |
| `streams` | live stream collections and their consumers' lag | lag over 1 h | lag over 6 h |
| `playbooks_executing` | `list_opencti_playbook_states` | running, no execution for 24 h, messages queued | last 10 executions over 50% failed |
| `stale_drafts` | `list_opencti_drafts`, untouched for 30 days | over 20 | |
| `data_volume_growth` | `get_opencti_entity_stats`: objects created in 24 h vs the 7-day daily mean | under 25% of the mean | 0 with a non-zero mean |
| `data_freshness` | newest object creation and update times | | nothing created or updated for 24 h with a non-zero 7-day mean |
| `data_completeness` | objects without author or marking, dangling relationships, sampled | over 5% | over 20% |
| `platform_errors` | `query_opencti_activity`: error entries of 24 h vs the 7-day mean | over 2 x the mean | over 5 x the mean |
| `works_in_error` | works in error in 24 h, counted platform-wide, never per source | over 10% of the works | over 30% |

## OpenAEV

| Check | Read | Warn | Fail |
|---|---|---|---|
| `platform_version_licence` | platform version, licence, settings sanity | licence under 30 days, a missing setting | licence under 7 days, version behind the supported range |
| `simulations_state` | `search_openaev_exercises`, `get_openaev_exercise`: failed or running past their end | any failed in 24 h | running 24 h past its end |
| `scenarios_scheduled` | `search_openaev_scenarios`: scheduled scenarios and their last start | | a scheduled start missed by over 1 h |
| `expectations_due` | expectations past their due time without a result | over 10% | over 30% |
| `findings_flowing` | `search_openaev_findings`: created in the last 7 days vs the previous 7 | 0 after a non-zero week | |
| `data_volume_growth` | simulations, scenarios, assets and findings created in 7 days vs the 4-week weekly mean | under 25% of the mean | |
| `data_completeness` | scenarios with no inject, simulations with no target, assets in no group (`search_openaev_assets`, `search_openaev_asset_groups`), injects with no expectation | over 10% | over 30% |
| `data_consistency` | assets duplicated by hostname or IP | any duplicate | |
| `platform_errors` | error entries and failed internal jobs of 24 h vs the 7-day mean | over 2 x the mean | over 5 x the mean |
| `module_link` | the configured link to OpenCTI: configured and its last call result, never the linked module's data | | configured and failing |

## OpenCRQ

| Check | Read | Warn | Fail |
|---|---|---|---|
| `platform_version` | `get_opencrq_platform_version` vs the supported range | | behind the supported range |
| `jobs_state` | `list_opencrq_jobs`, `get_opencrq_job`: failed or stuck, platform-wide | any failed in 24 h | running over 6 h |
| `crq_run_fresh` | `list_opencrq_crq_run_history`, `get_opencrq_crq_run`: the last successful run and its age | over 7 days | over 30 days, or none |
| `scenario_inputs` | `list_opencrq_risk_scenarios`, `get_opencrq_tef_input`, `get_opencrq_financial_profile`: scenarios missing an input | any | over 25% |
| `data_completeness` | scenarios never scored, or scored before their inputs last changed (`get_opencrq_crq_settings` for the scoring setup) | any | over 25% |
| `data_volume_growth` | risk scenarios and CRQ runs created in 30 days vs the previous 30 | 0 after a non-zero month | |
| `platform_errors` | failed internal jobs and error entries of 24 h vs the 7-day mean | over 2 x the mean | over 5 x the mean |
| `module_link` | the configured links to OpenCTI and OpenAEV: configured and their last call result, never the linked modules' data | | configured and failing |
