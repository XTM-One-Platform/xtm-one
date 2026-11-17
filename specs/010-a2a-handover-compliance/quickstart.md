# Quickstart: Validating A2A-Compliant Handover

**Plan**: [plan.md](./plan.md) | **Contracts**: [contracts/](./contracts/)

Four checks, one per phase, each provable without the others. Automated coverage
lives in `backend/tests/`; this is the end-to-end path a reviewer walks.

## Prerequisites

One deployment, federated with itself — a partner does not have to be somebody
else, because this platform's inbound half mints exactly the credential its
outbound half presents. [../../dev-docs/a2a-review-scenarios.md](../../dev-docs/a2a-review-scenarios.md)
says how to start it; migrations run at boot, so the new tables appear on start.

**The platform does not reload.** It imports code once, so restart it after
every change — and never apply a migration under a running instance, whose ORM
will keep selecting columns the database no longer has.

Expose an agent by binding an `oauth_a2a` integration to it (Tools tab), then
note the card URL and the client credentials from the integration.

```bash
TOKEN=$(curl -s -X POST http://localhost:4000/mcp/oauth/token \
  -d grant_type=client_credentials -d client_id=$CLIENT_ID -d client_secret=$SECRET \
  | ./backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
```

## 1. Delegated work keeps its task open (FR-001..FR-006)

Give the exposed agent a peer typed **delegate**, and prompt it to hand the work
off and answer immediately.

```bash
curl -s -X POST http://localhost:4000/a2a/$CARD_TOKEN -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"message/send",
  "params":{"message":{"messageId":"q1","parts":[{"text":"delegate this and reply"}]}}}'
```

**Expect**: `tasks/get` reports `working` while the child runs — not `completed`
(SC-001). When the child finishes, its output is among the task's artifacts
(SC-002). Cancel mid-flight and the child stops within a minute (SC-003).

Cross-check in the database that the child was actually running while the task
reported working:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml exec postgres psql -U copilot -d copilot \
  -c "select status, metadata_json->>'parent_run_id' from sub_agent_tasks order by created_at desc limit 3"
```

Leave one task delegating past the parked ceiling: it goes terminal with a
curated reason rather than staying pollable forever (SC-004).

## 2. A transfer carries the conversation (FR-007..FR-014)

Give the agent a peer typed **transfer**, submit, and note the `contextId` in the
response.

**Expect**: a second `message/send` carrying that `contextId` is answered by the
transfer target (SC-005), and the artifact's `respondingAgent` metadata names it.
Then try the three refusals — each must be a structured error or the uniform
not-found, never a silent new task:

- a `contextId` this deployment never issued;
- the same `contextId` presented with a *different* inbound registration's credential;
- a follow-up while the previous task is still `working`.

## 3. A partner that never polls still learns (FR-015..FR-022)

Confirm the card first: `capabilities.pushNotifications` is `true`.

```bash
curl -s http://localhost:4000/a2a/$CARD_TOKEN/.well-known/agent-card.json \
  | grep -o '"pushNotifications":[a-z]*'
```

Register a callback with `tasks/pushNotificationConfig/set` pointing at any
endpoint you can watch, supplying a `token` so you can tell a real notification
from noise. Then submit work and stop polling.

**Expect**: the callback fires on the terminal transition carrying the task's id
and state and no content (SC-006), with your `token` echoed back; a callback
pointing inside the deployment's own network is refused at registration. See
[contracts/a2a-push-notifications.md](./contracts/a2a-push-notifications.md).

## Automated

```bash
cd backend && CUSTOM_TOOL_SANDBOX=auto ./.venv/bin/python -m pytest tests/ -q -k a2a
```

Migrations get their own round-trip in CI (upgrade, re-run, downgrade); run it
locally against a scratch database before opening the PR.
