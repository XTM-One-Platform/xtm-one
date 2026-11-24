# OpenCTI health checks

Your playbook for one registered OpenCTI platform (the registration the trigger names). Run every check, in this order, read-only. Each check ends **pass**, **warn**, **fail** or **not run**, with the evidence you read: the tool, the objects, the values and the time you read them.

## Rules

- Look at the module itself, platform-wide. Never judge a specific connector, feed or third-party data source, and never name one in a verdict.
- A check whose tool is not among your read tools is **not run**, with the reason "tool not on the allow-list"; never infer it from another check.
- A value the module does not expose is **not run**, with the reason; never a pass.
- Thresholds below are the defaults; a lower threshold never applies.
- Absence of new objects (no new background task, no new report, no recent creation) is never evidence that a manager is stopped.
- A manager's `running` flag is not liveness. `get_opencti_managers` classes each manager stream (continuous) or cron (intermittent): a cron manager reads not running between two cycles, which is normal, and is never judged on `running`. Only an enabled stream manager that the tool names not running on all 3 reads, 60 s apart, counts as down; one read is a heartbeat snapshot. Never recommend a restart on a single read.

## Checks

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

## Report

Store every check with `record_module_check_report` before you end: its verdict, the evidence (tool, arguments, objects, values, time), a recommendation on warn and fail, a reason on not run. A warn or fail opens the check's agentic finding and a pass resolves it. Then write one line per check: name, verdict, the values read, the tool and the time. Then the recommendations you attached to open findings, and every check not run with its reason.
