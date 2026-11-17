# Emergency stop

The emergency stop halts every agentic activity at once. Runs, background
tasks, chat turns, flows and pending runner commands are cancelled, assignments
are paused, and — for the whole platform — AI is switched off so nothing new
starts. It is the button you press when an agent misbehaves, costs run away, or
an incident requires that no AI activity continues.

Everything the stop does is recorded, labelled where you look for it, and can be
lifted once the situation is under control.

## Who can press it, and how far it reaches

| You are | Scope you can choose | What it covers |
|---|---|---|
| Any user | `My activity only` | Everything that runs for you: your runs and the work they delegated, your background tasks, your in-flight chats, your flow runs, your pending runner commands, and the assignments you own or that run as you |
| An administrator | `My activity only` or `Whole platform` | The same for every user, plus evaluation runs and autonomous runs, plus the platform AI switch |

Work already in progress is stopped and does not resume, with two exceptions:
a runner command already executing on a machine cannot be recalled and runs to
its end, and an answer an agent is already writing for a channel bot, an MCP
client or the browser extension finishes under its own time limit. Cancelled
runs stay cancelled: lifting the stop re-arms schedules, it does not replay
what was cut.

## What stops and what keeps working

Stopped:

- runs in progress, queued or waiting for a human answer, and every run they handed work to
- background tasks delegated to other agents
- chat turns being generated in the XTM One chat and the chat widget
- chat turns waiting in the XTM One chat for the approval of a tool call — the turn ends, the prompt is withdrawn and the call does not run
- flow runs in progress
- runner commands not yet dispatched to a machine
- assignments — paused, so their schedules and triggers no longer fire
- with `Whole platform`: evaluation runs, autonomous runs, and AI for everyone

Untouched:

- everything that is not agentic: document processing, knowledge-base synchronisation, integration imports and exports, and flows with no agent step, whose triggers keep firing
- the triggers of flows: the runs in progress are cancelled, but a flow is not paused like an assignment. With `Whole platform`, a flow with an agent step waits for AI to be back; with `My activity only`, your flows' next trigger starts a new run
- runner commands already executing on a machine — they cannot be recalled,
  but none is sent to a machine again
- an answer already being written for a channel bot, an MCP client, the browser extension or the assistant embedded in OpenCTI, OpenAEV or OpenCRQ — it finishes under its own time limit; with `Whole platform`, no new one starts
- a tool call waiting for approval in an embedded assistant, or left for approval in a conversation whose turn already ended — it runs only if you approve it; with `Whole platform`, no decision is accepted until AI is back
- your data: agents, knowledge bases, flows, runs and traces stay readable

## Press the emergency stop

1. Open your profile menu (your name, top right) and choose `Emergency stop`.
2. As an administrator, choose the scope: `My activity only` or `Whole platform`.
3. Type the six-character code shown in the dialog. The red `Stop everything`
   button stays disabled until the code matches — a slip of the mouse is never
   enough.
4. Press `Stop everything`. The stop runs immediately; a notification lists
   what was cancelled and paused. If some running work could not be cancelled
   at once, the notification reads `Emergency stop partly applied`: the
   assignments are paused, AI is off for a platform-wide stop, and the
   follow-up passes cancel what they reach.
5. The dialog then asks why. The stop never waits for this: it is already done.
   Type the reason and `Save reason`, or `Skip`. The reason is written in the
   audit trail next to the stop and on every assignment the stop paused.

!!! tip "The stop is instant, the explanation comes after"
    An emergency does not wait for a form. That is why the reason is asked
    right after the stop, not before it.

## What you see afterwards

- **A red banner on every page** — `Emergency stop in effect`, with how many
  assignments are paused, whether AI is off, who pressed it, when and why. It
  carries a `Resume` button for whoever may lift the stop. A platform-wide stop
  shows the banner to every user, whether or not it paused any of their
  assignments; only an administrator can lift it, so regular users see the fact
  without a button.
- **Assignments** show a red `Emergency stop` badge instead of `Paused`, in
  the assignment page, the agent's assignment list and the status tile. The
  assignment page explains who pressed the stop, when and why.
- **Runs** cancelled by the stop carry the cause `Stopped by an emergency
  stop.` on their card and detail page.
- **Chat**, with the whole platform stopped, shows AI as switched off.
- **Logs** hold one entry per stop and per resume with the counts and the
  reason, plus one `pause` / `resume` entry per assignment.

While the stop is in effect, an assignment it paused cannot be re-enabled with
its ordinary on/off switch, and an agent cannot re-enable it through its tools.
The only way back is to lift the stop.

## Lift the emergency stop

Press `Resume` in the banner, or open your profile menu and choose
`Resume my activity` or, as an administrator, `Resume the whole platform`.

You are asked why. The reason is mandatory and is recorded on the resume entry
and on every assignment that is re-enabled.

Lifting the stop:

- re-enables only the assignments the stop paused — an assignment someone
  paused by hand stays paused
- while a platform-wide stop is in effect, only lifting that stop resumes
  anything: a user cannot resume their own activity underneath it
- lifting the platform-wide stop leaves in place a stop a user pressed for their
  own activity: that user lifts it themselves
- an assignment in two people's activity (one owns it, it runs as the other)
  stays paused while either of their stops is in effect: it comes back once
  both have lifted theirs, whichever paused it
- for the whole platform, sets the AI switch back to what it was before the
  stop — on, off, or `Deployment default` — so an administrator who had AI off
  keeps it off, and a platform started without AI stays without AI (see
  [Running without AI](running-without-ai.md)); a change made in `Settings`
  while the stop was in effect is kept
- does not resurrect cancelled work

## Prepare for an emergency without a session

Nobody may be signed in when the stop is needed: the on-call engineer has only
a terminal, or a monitoring system must press the button itself. For that, an
administrator creates the **emergency stop API token** in `Settings` →
`Security` → `Emergency stop`.

- `Generate token` creates it. It is shown **once**; only a hash is stored, so
  it can never be displayed again.
- `Print the procedure` opens a page for paper or PDF: what the stop does, when
  to use it, the exact command with the token, how to lift the stop afterwards,
  and a space for the people to call. Keep it with the on-call material. The
  page is produced in the language of your interface.
- `Rotate token` invalidates the current token and issues a new one. Every
  printed procedure carrying the old token becomes invalid and must be
  reprinted.
- `Revoke` disables the API path until a new token is generated. The button in
  the profile menu keeps working.

The section shows the token's prefix, who generated it and when, and when it
was last used.

!!! warning "Treat the printed page like a physical key"
    Anyone holding the token can stop the whole platform. It does nothing else —
    it cannot read or change anything — but rotate it as soon as a printed copy
    is lost or exposed. Every refused attempt to use it is recorded in the logs
    under the token owner's name.

Pressing the stop with the token always covers the whole platform and is
attributed to the administrator who generated the token. The command and the
responses are described in [Emergency stop API](emergency-stop-api.md).

## Next step

Read [Emergency stop API](emergency-stop-api.md) to press the stop from a
script or a monitoring hook, or [Running without AI](running-without-ai.md)
for what a platform with AI switched off can still do.
