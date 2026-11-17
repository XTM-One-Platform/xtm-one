# Webhook triggers

A webhook trigger lets another system start an assignment. Your SIEM raises an alert, your SOAR runs a playbook, your ticketing tool opens a case — each of them can post to a URL, and the agent starts working on what it received.

This page covers where to find that URL, how to authenticate a call to it, and how to wire it up from a SOAR playbook.

## Find the URL and the secret

Open the agent, find the assignment in the `Assignments` card, and open it. On the `Trigger` card of an assignment whose `Trigger type` is `Webhook` you will see:

- `Webhook URL` — the address the sending system posts to. It already contains the assignment identifier, so there is nothing else to configure.
- `Webhook secret` — the credential the sending system must present. Every webhook assignment gets one automatically.
- `How to trigger` — a ready-to-paste request you can copy, adapt, and test with before you touch the other system.

!!! note "Who can see the secret"
    The secret is only shown to people who can manage the assignment. If the `Trigger` card shows the URL but no secret, ask whoever owns the assignment for it.

## Authenticate the request

Send the secret in one of these headers — **verbatim**, except in the `Authorization` form, which takes the `Bearer ` prefix:

| Header | Value |
| --- | --- |
| `X-Webhook-Token` | the secret, verbatim |
| `X-Webhook-Secret` | the same thing, under the name the `Webhook secret` label suggests |
| `Authorization` | `Bearer <secret>`, for senders that only offer standard authentication fields |
| `X-OpenCTI-Token` | the original spelling, still accepted so existing senders keep working — use one of the names above for anything new |

This is what almost every SIEM and SOAR webhook action can produce, and it is the scheme to reach for first. A secret with non-ASCII characters is matched whether the sender writes the header in UTF-8 or in latin-1.

If your sender can compute an HMAC over the request body, you can sign it instead and keep the secret off the wire: put the hex HMAC-SHA256 of the raw body in `X-Signature`. Provider-specific signatures — GitHub, Slack — are recognized as well.

!!! warning "A correct URL is not enough"
    A request that carries none of these headers is refused with `401`, even when the URL and the secret are both right. The most common cause is a header sent under a name that is not on the list above. Every webhook assignment is created with a secret, and administrators can additionally require one platform-wide — see [`Configuration`](../deployment/configuration.md).

## Trigger from a Google SecOps SOAR playbook

The native webhook action in a SecOps SOAR playbook only offers a URL and a token, with nowhere to put a header. Use the generic **Execute HTTP request** action instead, configured like this:

| Field | Value |
| --- | --- |
| `Method` | `POST` |
| `URL Path` | the `Webhook URL` from the `Trigger` card |
| `Headers` | `{"Content-Type": "application/json", "X-Webhook-Token": "<the webhook secret>"}` |
| `Body Payload` | the JSON you want the agent to receive, for example `{"case_id": "[Case.case_id]", "alert_name": "[Alert.alert_name]"}` |
| `Fail on 4xx/5xx` | `true` |
| `Request Timeout` | `120` |

!!! warning "Do not paste the secret in clear"
    Store it in the SecOps SOAR credential store (or an environment parameter) and reference it as a placeholder instead of the literal value. Playbooks are exported, cloned between environments and often committed, and this credential is long-lived and travels on the wire with every delivery. Anyone who obtains a copy can authenticate **any** body against your assignment until someone rotates the secret. An `X-Signature` HMAC is bound to the exact body it signed and never puts the secret on the wire at all, so senders that can compute one should prefer it.

Three things to get right:

- **Write plain JSON in `Headers` and `Body Payload`.** The field expects JSON as-is. Escaping the quotes — `{\"Content-Type\": \"application/json\"}` — makes the playbook reject the value before the request is ever sent, with a message about an invalid JSON structure.
- **Keep the placeholders inside the quotes.** `"[Alert.alert_name]"` is valid JSON; an unbalanced quote around a placeholder is not, and the request fails for a reason that has nothing to do with XTM One.
- **Leave `Fail on 4xx/5xx` on.** It is what makes a refused or throttled delivery visible in the playbook instead of passing for a success.

## Event matching

A trigger that selects events - an integration event such as GitHub `pull_request`, or the `events` list of a webhook trigger - runs on exactly the events it selects:

- `pull_request` runs on `pull_request` and its actions (`pull_request.opened`, `pull_request.closed`, ...), never on `pull_request_review` or `pull_request_review_comment`, whose names only start with it.
- An `events` entry works the same way: `issues.opened` matches `issues.opened`, never `issues.opened_by_bot`.

A trigger saved on a version before exact matching keeps the prefix match it always had, so nothing that ran before stops running: `pull_request` also runs on `pull_request_review`, an `events` entry also matches any event whose name starts with it, and a GitHub trigger on its dedicated URL runs on every delivery it receives, `ping` included. It keeps that through every edit that leaves its events alone - a new name, prompt, delay, filter or integration - and through every copy: duplicate, import, catalog clone.

The `Trigger` card says which mode an assignment uses: `Exact event matching` with what it runs on, or `Legacy event matching` with the events it also runs on because of the prefix match. A legacy trigger switches to exact matching when you:

- choose `Use exact matching` on its `Trigger` card, or
- choose its events again in the trigger editor - another event, provider or trigger type. The editor says so before you save.

Administrators can apply exact matching to every trigger at once: **Settings > Access and security** > `Exact event matching for every trigger`, or `WEBHOOK_EXACT_EVENT_MATCHING=true`, which wins. See [`Configuration`](../deployment/configuration.md).

!!! note "Triggers the agent builder created before this version"
    The agent builder used to store an integration event under `event_type`, a key the platform does not read: such a trigger runs on every event its provider delivers by webhook, and a polled one (a new email, a new message) never runs. It keeps behaving that way until you choose `Use exact matching` on its `Trigger` card: it then runs only on the event it names, and a polled one starts with its next new event. The `Trigger` card and the security audit point these triggers out.

## When something does not arrive

Read the response the sending system got:

| What you see | What it means |
| --- | --- |
| `401 Missing webhook signature…` | No recognized credential header. Check the header **name** first — the secret is usually fine. The rest of the message lists the headers you can send. |
| `401 Invalid webhook token` | A token header is right, its value is not. Copy the secret again from the `Trigger` card. |
| `401 Invalid webhook bearer token` | Same, for the `Authorization: Bearer` form. |
| `401 Invalid webhook signature` | The body was signed with a different secret, or the signature was computed over a modified body. |
| `429` | Too many deliveries, or too many already waiting. Retry after the delay given in the response. |
| `"status": "ignored"` | The delivery was accepted but matched nothing — the assignment is disabled, deleted, or not a webhook assignment. |
| `"reason": "event not selected"` | A GitHub delivery to the dedicated URL of an integration-event assignment that matches exactly, for an event its trigger does not select. Check the event on the `Trigger` card, or the events the GitHub webhook sends (see [Event matching](#event-matching)). |

Everything that did start is in [`Operations`](work-history.md), including runs still waiting behind an active one. An assignment runs up to `ASSIGNMENT_MAX_CONCURRENT_RUNS` deliveries at once (10 by default, or its trigger's `max_concurrent_runs`), so a delivery arriving while they are all going is queued rather than lost, and starts as soon as one ends. A pushed integration event arriving then - a GitHub delivery to an integration-event assignment, for example - is skipped instead, unless an administrator turned on `INTEGRATION_EVENT_QUEUE_ENABLED`; a polled one (an email, a Teams or Slack message) always waits in the queue. See [Webhook delivery](../deployment/configuration.md#webhook-delivery).

Administrators can tune the delivery limits, the queue depth, and the de-duplication window — see [`Configuration`](../deployment/configuration.md).

## Next step

Continue with [`Operations`](work-history.md) to follow what your triggered runs actually did, with [Assignments and triggers](assignments.md) for the other triggers, or with [`Agents`](agents.md) if you still have to create the agent behind the assignment.
