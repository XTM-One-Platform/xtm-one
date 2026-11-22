# OpenCTI health checks

Your playbook for one registered OpenCTI platform (the registration the trigger names). Run every check, in this order, read-only. Each check ends **pass**, **warn**, **fail** or **not run**, with the evidence you read: the tool, the objects, the values and the time you read them.

## Rules

- Look at the module itself, platform-wide. Never judge a specific connector, feed or third-party data source, and never name one in a verdict.
- A check whose tool is not among your read tools is **not run**, with the reason "tool not on the allow-list"; never infer it from another check.
- A value the module does not expose is **not run**, with the reason; never a pass.
- Thresholds below are the defaults; a lower threshold never applies.

## Checks

| Check | Read | Warn | Fail |
|---|---|---|---|
| `platform_version_licence` | `get_opencti_platform_version`, licence expiry, base URL set | licence under 30 days, a missing setting | licence under 7 days, version behind the supported range |
| `managers_running` | platform managers (rule engine, retention, expiration, history, notification, sync) | | an enabled manager not running |
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

One line per check: name, verdict, the values read, the tool and the time. Then the recommendations you attached to open findings, and every check not run with its reason.
