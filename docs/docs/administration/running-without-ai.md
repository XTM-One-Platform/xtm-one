# Running without AI

You can run the platform with AI switched off. No model or embedding provider is
contacted, no AI work is scheduled, and nothing leaves the deployment for an AI
service. Everything that does not need a model keeps working.

This suits a platform installed before the AI capability is bought, a deployment
where policy forbids sending data to a model, and an air-gapped install with no
provider to reach.

!!! info "No licence is required"
    A platform running without AI needs no licence. You can install it, hand it
    to your users, and buy the AI capability later.

## What keeps working

- Deterministic flows, their triggers and their schedules
- Integration syncs, importing and exporting as usual
- Sign-in and single sign-on, users, groups, roles and invitations
- Platform settings, branding, audit logs, health and metrics
- Every existing agent, knowledge base, flow, run and trace, readable as before

## What is unavailable

- Chat, and every agent or assistant that answers with a model
- Creating agents, knowledge bases, prompts, skills and templates
- Knowledge-base indexing, and searching a workset
- Evaluations, and the MCP endpoints

AI areas stay listed and reachable in the navigation, marked unavailable rather
than removed, so you can still see what is configured. AI actions are visibly
disabled and explain why instead of failing when used.

To switch AI off for some people only, use a group's `AI access` card instead (see
[Switching AI off for a group](users-and-groups.md#switching-ai-off-for-a-group)).

## Turning AI off or on

An administrator changes this from `Settings` → `Parameters`, under
`Artificial Intelligence`. No restart is needed.

The setting has three values:

- `Enabled` — AI runs, whatever the deployment default is
- `Disabled` — AI does not run, whatever the deployment default is
- `Deployment default` — follow the `PLATFORM_MODE` the platform was started
  with

`Deployment default` is a distinct choice, not the same as `Disabled`: it hands
the decision back to the deployment, so a later change to `PLATFORM_MODE` takes
effect.

### Turning AI on

Saving provisions the built-in content that a normal startup would create, so the
first save after enabling takes a moment. Wait for it to finish rather than
reloading — an interrupted save leaves the platform enabled with some content
still missing, and saving again completes it.

You also need at least one model provider configured before agents can run. Add
one from `Settings` → `AI Models`.

### Turning AI off

New AI work stops within one scheduler interval and AI actions begin refusing
immediately.

!!! warning "Runs already in progress finish"
    Work already running is left to complete under its own time limit rather
    than being cancelled, so you may see AI activity for a few more minutes
    after saving. Cancelling mid-run would leave half-written runs and traces.

Work that was scheduled but had not started yet does not start: it is held.
An assignment run waiting on its delay, a run resumed from a human-review pause
(the answer you gave is kept), a background task that had not begun and the
pending runs of an evaluation are shown as **Held** and run by themselves within
about five minutes of AI being turned back on. A conversation waiting on a held
background task hears back when that task finally runs. Held work is never
charged. Deliveries waiting in an assignment's queue stay queued until AI is on
again.

A hold is bounded: work held for longer than `AI_UNAVAILABLE_HOLD_MAX_SECONDS`
(one week by default) is recorded as failed with the cause
`AI disabled on the platform` and a message saying how long it waited. Set it to
`0` to fail such work at its start instead of holding it. See
[Configuration](../deployment/configuration.md#platform-mode).

Nothing is deleted. Agents, knowledge bases, flows, runs and traces are all kept
and stay readable, and turning AI back on needs no repair.

!!! tip "To stop work in progress as well, use the emergency stop"
    Turning AI off stops new work only. The [emergency stop](emergency-stop.md)
    with the `Whole platform` scope cancels the runs in progress too, pauses
    every assignment, and switches AI off in the same move. Lifting it sets the
    switch back to what it was before the stop.

## Setting the deployment default

`PLATFORM_MODE=ai_disabled` starts the platform with AI off, and is the right
setting for an install that should never contact a provider unless someone
deliberately changes it. See
[Configuration](../deployment/configuration.md#platform-mode).

A platform started this way seeds no AI content at all: no built-in agents,
assignments, templates, skills or knowledge bases are created, and the startup
log states what was skipped. They are created the first time AI is enabled.

## Flows

A flow runs as long as none of its steps needs AI. Flows built from schedules,
webhooks, HTTP requests, notifications, conditions and scripts are unaffected.

A flow containing at least one AI step does not start. It is recorded as skipped,
with the reason on the run, so you can see in `Operations` that it was held back
rather than silently missed. No part of it executes.

Its triggers wait rather than being used up: a schedule that comes due and the
integration events that arrive meanwhile are picked up once AI is available
again (a schedule catches up with one run, not one per occurrence it missed), a
webhook delivery is answered with a `429` its sender retries, and a work item
stays on its queue. Nothing has started, so this is not the hold described
above: `AI_UNAVAILABLE_HOLD_MAX_SECONDS` does not end the wait.

In the flow editor, AI steps appear locked rather than absent, and a flow that
already contains one tells you it will not run. Removing the AI step lets the
flow run again on its next trigger — the decision is made from the flow as it is
at that moment, not from a stored verdict.

!!! note "A flow that hands off to another flow"
    If a flow calls another flow, the AI steps of the called flow count too. A
    deterministic flow that hands off to an AI-bearing one is held back.

## Next step

To configure model providers once AI is enabled, see
[Settings](settings.md). For what the platform shares with an AI provider and
how to control it, see [AI governance](ai-governance.md).
