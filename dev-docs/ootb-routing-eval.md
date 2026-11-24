# Out-of-the-box routing eval

How a simple question reaches the agent that answers it on a fresh platform, and the eval that keeps that path from getting longer.

## The path

1. The surface asks the intent catalog for `global.assistant`: OpenCTI with `vertical=cti`, OpenAEV with `vertical=aev`, OpenCRQ with `vertical=crq`; XTM One's own chat uses the General Assistant (the CTEM Assistant in XTM One mode, `services/provisioning.py`).
2. The catalog (`api/v1/intents.py::get_intent_catalog`) returns the vertical and universal bindings sorted by effective priority, descending; the first is the default entry agent.
3. The entry agent answers, or hands over (`transfer`, `delegate`, `consult`) through the seeded `handover_type_slugs` of `backend/app/agents/builtin_agents.py`. Each hop is one more model turn.

## The deterministic half (CI)

`backend/tests/test_ootb_routing_eval.py` replays step 2 on the seeded bindings and walks step 3 breadth-first. It pins:

- one entry agent per surface, and the module assistants' `global.assistant` bindings on their vertical (never removed, moved or disabled);
- for each golden task, the hop count from the surface's entry agent to the agent that does the work, at or under its baseline;
- the reach of every chat-enabled specialist from its surface's entry agent;
- that a baseline is lowered in the same pull request that lowers a hop count (`test_baseline_is_not_stale`).

A routing change edits the seeded agents, runs the file and lowers the baselines it improved. A raised hop count fails CI.

## Baseline

Entry agents: each module chat opens on its module assistant (`global.assistant` at 5 on its vertical), above the universal `ctem-assistant` (4), which stays the XTM One entry and one transfer from every module assistant. The OpenAEV and OpenCRQ assistants transfer to `opencti-assistant` directly, so a threat intelligence question asked there takes one hop, not two through the hub.

The CTEM Assistant transfers directly to `opencti-dashboard-creator` and `openaev-scenario-creator` (#10800), so a dashboard or a scenario asked in XTM One takes one transfer instead of two through the module assistant.

Before: main 81aefae9 (7 Nov 2026), `ctem-assistant` at 10 was the entry of every surface.

| Surface | Golden task | Target agent | Hops before | Hops now |
|---|---|---|---|---|
| OpenCTI | create a dashboard with widgets | opencti-dashboard-creator | 2 | 1 |
| OpenCTI | add a widget (the widget creator is spawned by the dashboard creator, never a chat agent) | opencti-dashboard-creator | 2 | 1 |
| OpenCTI | search an entity, create an indicator or relationship | opencti-assistant | 1 | 0 |
| OpenCTI | summarize a report, entity insights | opencti-entity-insights | 2 | 1 |
| OpenCTI | threat hunt | opencti-threat-hunt-creator | 2 | 1 |
| OpenAEV | create a scenario | openaev-scenario-creator | 2 | 1 |
| OpenAEV | publish a scenario | openaev-scenario-publisher | 3 | 1 |
| OpenAEV | explain an expectation result | openaev-assistant | 1 | 0 |
| OpenAEV | threat intelligence question | opencti-assistant | 1 | 1 |
| OpenCRQ | control gap, risk scenario, assessment summary | opencrq-assistant | 1 | 0 |
| OpenCRQ | OpenCTI dashboard | opencti-dashboard-creator | 2 | 2 |
| XTM One | posture, CVEs to fix Monday, small talk | ctem-assistant | 0 | 0 |
| XTM One | OpenCTI dashboard / OpenAEV scenario | creator agents | 2 | 1 |

The priority is a builtin binding's seeded value: the seed resyncs it on every boot (`seed.py`, phase 2), so existing installs take it without a seed version bump; a customer's own binding is never touched.

Copilot mode seeds no module assistant and no CTEM Assistant: its surface is the General Assistant alone, out of this graph.

## The live half (lab)

Success, tokens, wall time and tool calls need a model. Run the golden tasks on a lab with a live model (`sandbox_start_service lab-live`, the Copilot's LLM proxy on a short-lived key minted per lab, `dev-docs/reference/agentic-labs.md`) or on staging with the runner `.xtm-one/lab/routing_eval.py` (standard library only):

```
python .xtm-one/lab/routing_eval.py --base http://127.0.0.1:8100 \
    --email lab@filigran.io --password lab-only-password [--surface xtm_one] [--task dashboard] [--out eval.json]
```

From an agent, the runner reaches a lab only from inside it: a check container has no route to a lab pod. Start the lab, then run the declared check `lab-routing-eval` in it (`.xtm-one/checks.toml`: the command above with the lab's throwaway sign-in, default `--task "create an OpenCTI dashboard from XTM One"`, args replace it):

```
sandbox_start_service(checkout="xtm-one", service="lab-ootb-live")            -> service_id lab-...
sandbox_run_command(checkout="xtm-one", check="lab-routing-eval", service_id="lab-...")
sandbox_run_command(checkout="xtm-one", check="lab-routing-eval", service_id="lab-...", args=["--surface", "xtm_one"])
```

The answer is the exit code and the JSON table on stdout (the end kept past 20,000 characters; `--out` writes it whole in the lab's copy). A run answers within 270 s: pick tasks with `--task` or `--surface` rather than the whole table at once. `--task` matches, case-insensitively, a golden task's label (`"summarize a report"`) or its prompt (`"Summarize the latest report."`); filters that select no task exit 5 before the sign-in, with the labels on stderr, so a mistyped filter never passes with `ran: 0`. One task's turn may take `--task-timeout` seconds (default 150): past it the runner stops waiting, keeps the row with `timed_out: true` (a failure, `error` "no answer within N s", hops and tokens as the traces held them), counts it in the summary's `timed_out` and goes on to the next task, so a turn that waits on a delegation never costs the whole call.

Every run also keeps its table in the lab's copy at `.xtm/routing-eval/last.json`, rewritten after each task (`"partial": true` until the run ends). When the calling turn is handed back while a `lab-routing-eval` call is in flight (a rollout), the resumed turn does not ask the tasks again, which would spend the lab's daily LLM proxy quota a second time: it reads that table with the declared check `lab-routing-eval-last` (`routing_eval.py --last`, no sign-in, no task, no LLM call):

```
sandbox_run_command(checkout="xtm-one", check="lab-routing-eval-last", service_id="lab-...")
```

Exit 0: the last run ended and the table is complete; exit 1: it stopped mid-way and the table lists the tasks it finished; exit 4: no run finished a task in this lab.

To prove a step spent no LLM call (or exactly the calls it should), read the lab model key's LLM proxy quota around it with the declared check `lab-model-quota` (`routing_eval.py --quota`): it asks `GET /v1/quota` of the proxy with `LAB_MODEL_API_KEY`, no completion, no counter moved, and prints `{"live": true, "used", "limit", "period", "period_key", "resets_at", "per_model"}` (`{"live": false}` on a stub-model lab; exit 0 either way). The retest recipe: `lab-model-quota`, `lab-routing-eval-last`, `lab-model-quota` again: `used` is equal; around one `lab-routing-eval` task it grows.

```
sandbox_run_command(checkout="xtm-one", check="lab-model-quota", service_id="lab-...")
```

Readiness comes first. `sandbox_service_status` can answer a `lab-ootb-live` lab ready minutes before its own OpenCTI registers (`seeded live OpenCTI integration` at about 330 s); a task asked in between reaches a specialist that answers "the OpenCTI integration isn't connected", a readiness failure, not a routing one. Before the first task, the runner reads `GET /integrations` until every suite platform the chosen tasks act on (their surface, or the product their label names: `create an OpenCTI dashboard from XTM One` needs OpenCTI) is a company-managed integration, for up to `--wait` seconds (default 120). Past it, it runs no task, writes `{"readiness": ..., "not_ready": "the lab's OpenCTI is not registered yet ...", "tasks": []}` and exits 2: run the check again a minute later. Every report carries `readiness` (`ready`, `waited_seconds`, the integration id per platform). `--out` creates its directory, so `--out .xtm/eval.json` works on a fresh lab.

It reads `GOLDEN_TASKS` and `ENTRY_AGENTS` from `backend/tests/test_ootb_routing_eval.py`, opens one conversation per task on the surface's entry agent, sends the task's prompt (`PROMPTS` in the runner), and reads the conversation's traces and spans back: per task the agent it ended on, `handovers` (calls of `transfer_to_agent`, `consult_agent`, `spawn_background_task`), generations, input and output tokens, tool calls and errors, wall seconds, and `success` (ended on the expected agent, no error, hops within the baseline). A run's trace and spans are written as the run ends, so the runner re-reads them every 3 s for up to `TRACE_SETTLE_SECONDS` (45 s) until a generation is recorded; a turn still without one is `unmeasured` (`success` null, the reason in `unmeasured`), never a pass on the entry agent it falls back to. The summary counts `passed`, `failed` and `unmeasured`. The exit code is 0 when every task that ran passed. Record the JSON table next to the deterministic one in the routing pull request.

`verified` stays `null` unless the run reads the deliverable back: with `--verify`, a task whose prompt names a dashboard (`Create a dashboard named 'Routing eval' ...`) reads the lab's OpenCTI GraphQL (`workspaces`, type dashboard, created since the task started; default `http://127.0.0.1:4000` with the lab OpenCTI's throwaway admin token, `--opencti-url` / `--opencti-token` otherwise) for up to `--verify-wait` seconds (default 90): `verified` true with `dashboard_id` and `widgets` when the dashboard holds a widget, else false, which fails the row. A creator agent that shows its plan and asks for approval gets it from `--confirm TEXT`, sent as a second turn of the same conversation once the first answered: `lab-routing-eval --task "create a dashboard with widgets" --surface opencti --confirm "Build it as it is." --verify`. The Dashboard Creator builds widgets through delegated background tasks, which the worker's orchestration reconciler starts: `lab-ootb-live` runs the SAQ worker beside the backend (`LAB_WORKER = "1"`, #11023), so a delegated child starts within seconds as in production. `sandbox_start_service lab-ootb-live` is `lab-live` plus an OpenCTI of its own in the same pod (its search engine, broker, Redis and S3 on ports apart from the lab's stores), each product on an image key the operator allows in `AGENTIC_LABS_IMAGES` (`opencti`, `opencti-search`, `opencti-broker`, `opencti-redis`, `opencti-s3`; a platform without them refuses the start with the key named; the images of OpenCTI's own compose fit: `opencti/platform` 6.x or 7.x (the service starts `build/back.mjs` when the image has it, else `build/back.js`), `opensearchproject/opensearch` (the search service starts its entrypoint), `rabbitmq`, `redis`, `pgsty/silo` or `minio/minio`). The OpenCTI holds the lab's registration token and registers itself with the lab's backend through `/platform/register`, so its integration, intents and per-user tokens are the product's own; the seed skips its `.invalid` OpenCTI row (`LAB_OPENCTI = "live"`) and logs `seeded live OpenCTI integration: <url>` once the real one appears (up to 7 minutes: the registration manager fires 30 s after boot, then every 5 minutes). About 6 GB; started only on purpose. A task that acts on OpenAEV still measures routing, hops and cost only.

### Last live run

11 Nov 2026, main 453db74c, `lab-ootb-live` (its OpenCTI registered after 206.7 s), one `lab-routing-eval` call per surface or per two or three tasks, every task measured (0 unmeasured); XTM One from the 01:25Z run on c1a41589 (5/5). Before #10985:

| Surface | Ran | Passed | Failed | Input tokens | Seconds |
|---|---|---|---|---|---|
| XTM One | 5 | 5 | 0 | 1,328,452 | 86.9 |
| OpenCTI | 7 | 4 | 3 (add a widget: right agent, wrong golden row; summarize a report and entity insights: answered by opencti-assistant, 0 handovers) | 1,624,153 | 155.9 |
| OpenAEV | 4 | 2 | 2 (publish a scenario: no edge to the publisher; TI question: consult then transfer to ctem-assistant) | 1,455,771 | 132.1 |
| OpenCRQ | 4 | 4 | 0 | 999,833 | 86.1 |

After #10985 (lab from its head): OpenAEV create a scenario 1 hop, 60,653 in, 7.3 s (was 115,559, 12.9 s); publish a scenario 1 hop to the publisher, pass; the threat intelligence question 1 transfer to opencti-assistant, 95,593 in, 12.0 s (was 2 handovers, 619,833 in, 65.2 s, fail); OpenCTI add a widget pass (1 hop). Summarize a report and entity insights stay open: a persona line telling the OpenCTI Assistant to delegate first made the turn wait past 270 s on a delegation the lab abandoned (`Background task ... abandoned: no orchestration execution`), against about 20 s answered directly, so it was not kept.

After #10995 (lab from its head on main 67b17fa2, `--task-timeout 150`): the OpenCTI Assistant's edge to `opencti-entity-insights` is a transfer, not a delegate. Summarize a report: 1 transfer to opencti-entity-insights, pass, 233,633 in, 20.3 s (was 0 handovers, answered by opencti-assistant, 306,876 in, fail). Entity insights: 1 transfer, pass, 280,652 in, 22.8 s (was 0 handovers, 221,523 in, fail: the specialist reads more of the entity than the assistant's short answer did). Unchanged on the same lab: search an entity 0 hops, pass (466,127 in, 19.4 s); create a dashboard with widgets 1 hop, pass (75,983 in, 14.0 s). OpenCTI is then 7 of 7 on the golden tasks.

Output format after a transfer: `opencti-entity-insights` is seeded with `output_format` `html` for its `cti.*` intent calls, and a transfer makes its answer the chat message, which the chat renders as Markdown with no raw HTML. A run handed a chat by a transfer or a handover therefore answers in Markdown when its agent's format is `html` (`chat_handover_output_format` in `backend/app/services/output_format.py`); intent calls, consults and background tasks keep HTML. The runner fails a row whose chat answer carries raw HTML tags (`raw_html` and `format_error` in the row), whatever the route.

To check an agent's own output format without a transfer, ask the task of that agent directly with `--entry SLUG` and assert the format with `--expect-format html|markdown`:

```
sandbox_run_command(checkout="xtm-one", check="lab-routing-eval", service_id="lab-...", args=["--entry", "opencti-entity-insights", "--task", "summarize a report", "--expect-format", "html"])
```

`--entry` keeps the `--task` / `--surface` filters, opens each conversation on that agent (`entry_agent` = the slug), and expects the turn to end on it with no handover (`expected_agent` = the slug, `baseline_hops` 0); a slug the lab did not seed is a skipped row. `--expect-format html` passes a row only when its answer carries at least one HTML tag; `markdown` (the rule without the flag) fails it on any raw tag. The row names `expected_format`, and `format_error` when the answer breaks it. Each task is still one chat turn: no quota beyond the tasks asked.

XTM One posture and "which CVEs do we fix Monday" (lab from main cb8abe11, 11 Nov 2026): both pass in 0 hops on ctem-assistant; posture 631,344 in, 7 generations, 8 tool calls of which 7 errors, 35.1 s; CVEs Monday 353,116 in, 4 generations, 5 tool calls of which 2 errors, 19.5 s. Every error is `Could not connect to OpenAEV` or `Could not connect to OpenCRQ`: `lab-ootb-live` runs a real OpenCTI only and registers OpenAEV and OpenCRQ at `.invalid` URLs (`SUITE_PLATFORMS` in `.xtm-one/lab/lab.py`), so these tool errors and the generations spent on them are lab artefacts, not a persona or tool-set defect. A persona or tool trim for these two tasks cannot be measured on this lab; it needs a lab with a reachable OpenAEV and OpenCRQ. The entity insights persona now scopes its HTML link and `<meta>` rules to intent calls, so a transferred chat follows its Markdown Output Format throughout.

Earlier run, 11 Nov 2026, main 936378fa, `lab-ootb-live`, one `lab-routing-eval` call per surface, before the trace re-read above: 20 tasks, 8 passed and 4 failed on recorded traces, 8 read with no trace at all (0 generations after a 6 to 33 s turn, so the runner credited the entry agent: 6 reported passes and 2 reported failures that were not measured).

| Surface | Ran | Passed (measured) | Failed (measured) | No trace | Input tokens | Seconds |
|---|---|---|---|---|---|---|
| XTM One | 5 | 4 | 0 | 1 (posture question) | 685,582 | 82.7 |
| OpenCTI | 7 | 2 | 2 | 3 (search an entity, create an indicator, summarize a report) | 849,893 | 156.7 |
| OpenAEV | 4 | 0 | 2 | 2 (publish a scenario, explain an expectation result) | 94,897 | 114.9 |
| OpenCRQ | 4 | 2 | 0 | 2 (risk scenario, assessment summary) | 430,188 | 88.3 |

Measured failures: OpenCTI "add a widget to a dashboard" ends on opencti-dashboard-creator, not the widget creator; OpenCTI "entity insights" is answered by opencti-assistant itself with search tools; OpenAEV "create a scenario" is answered by openaev-assistant in one generation with no transfer; OpenAEV "a threat intelligence question" transfers to ctem-assistant instead of opencti-assistant. XTM One reaches both creators in one hop (#10805), OpenCRQ reaches the OpenCTI dashboard creator in two.

Run of 10 Nov 2026, main 7067f601 (build 1.261110.0-dev.e6440f1, which carries the Anthropic bridge tool forwarding of #10777), `lab-ootb-live` with its own OpenCTI 7.261106.0 registered (`seeded live OpenCTI integration` at 329.7 s), check `lab-routing-eval`, exit 0 in 22.3 s:

| Surface | Task | Entry | Final (expected) | Hops (baseline) | Handover tools | Generations | Input tokens | Output tokens | Tool calls (errors) | Seconds | Verified | Success |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| XTM One | create an OpenCTI dashboard from XTM One | ctem-assistant | opencti-dashboard-creator (opencti-dashboard-creator) | 2 (2) | transfer_to_agent x2 | 4 | 142,480 | 1,451 | 3 (0): search_knowledge 1, transfer_to_agent 2 | 21.3 | null | true |

The route takes the two hops of the baseline; most of the cost is input tokens (about 35,600 per generation, the tool definitions of each agent included). `verified` is still `null`: the runner checks the route, not the dashboard read back from OpenCTI.

The lab's OpenCTI starts through `.xtm-one/lab/opencti-start.sh`: it waits for each dependency port, for the search cluster at yellow and for a Redis write before OpenCTI's first-start initialisation, which exited with code 1 right after `[INIT] New platform detected` when it raced a store still starting (#10810). A failed start names its cause in `sandbox_service_logs(service_id, service="lab-opencti")`: the `lab-opencti:` lines say what it waited for and `lab-opencti: node exited with code N` closes it.
