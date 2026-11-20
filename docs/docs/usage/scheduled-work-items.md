# Scheduled work items

Ask any agent to do something once, later: "post this in the EMT channel tomorrow at 9:00", "send me the summary of this thread on Friday at 17:00". The agent schedules a **scheduled work item** and the platform runs it at that time by itself. There is nothing to set up: no assignment, no flow, and no built-in tool to enable. Recurring work stays with [assignments](agents.md).

## Schedule from a chat

Every agent can schedule, list, cancel and reschedule its own one-off actions, whatever its built-in tool selection. These four tools cannot be removed from an agent: an agent's tool deny list refuses them. Only an administrator can turn scheduling off, for the whole platform (see [Turn scheduling off](#turn-scheduling-off)).

A scheduled action takes one of two forms:

| Form | What runs at that time |
|---|---|
| **Exact** | One of the agent's tools, exactly as it was prepared when you asked: the message text, its mentions and its target are sent as stored, with no model involved. Use it when the content is final. |
| **Instruction** | The agent runs the instruction in a background run, with its tools, as when you chat with it. Use it when the agent should work something out at that time ("check the open incidents and post a summary"). |

The action runs **as you**: with your rights on the agent and with the integrations the agent uses for you. When it is scheduled from a conversation, its outcome is posted back into that conversation, and you receive a notification. Clicking the notification opens the run of the item, where the outcome and its proof are shown; an item that never ran opens the conversation that scheduled it.

Right after the agent schedules it, the conversation shows a card with:

- the time it runs, in your time zone (and in UTC),
- the action (exact tool or instruction) and where it goes,
- a preview of the content (at most 280 characters, cut with an ellipsis; the whole content stays in the item),
- **Cancel** and **Reschedule** buttons.

!!! note "Times always carry a time zone"
    A scheduled time is always stored with its UTC offset. A time without one is refused rather than read as UTC, and every message gives the time in your time zone and in UTC. Set your time zone in your [profile](my-profile.md) so the agent and the card use it. When the offset an agent wrote is not your time zone on that date (for example across a daylight saving change), the agent is told, so it can reschedule.

## What happens at the time

The platform checks, every minute, for scheduled items that are due, and runs each one once:

1. The item is claimed, and one run is created for it. The run shows in **Operations > Executions** and in the agent's **Activity** tab, labelled **Scheduled** and linked to its work item.
2. Before anything is sent, everything is checked again: that scheduling is still on, that the agent still exists and is enabled, that your account is active and can still use the agent, that agentic AI is available on the platform, and, for an exact action, that the tool is still available to the agent for you (its integration connected and shared with you, the tool not denied or held for approval). If a check fails, nothing runs and the item is **blocked** with the reason.
3. The action runs. A successful one ends **done**, with what the tool or the agent returned as proof (for example the message id and link). A failed one ends **blocked**, with the error.

A failure is retried only when the action provably did not reach its target (the platform could not read whether AI is available, the target answered "too many requests", the model provider was unreachable before any tool ran), at most a few times. A refused check, an invalid payload and an error the tool returned are never retried, so a message is never sent twice. If the platform restarts while an action runs (a deployment, for example), the action goes on by itself, with nothing to do on your side. An instruction resumes where it stopped. An exact action that only reads is run again; one that may change something is never sent again blind: the agent that scheduled it gets it again, reads first whether it was already done, and does it only if it was not. The item says how many times it was resumed, and it ends **done** as usual. It is blocked only for a failure that is not a restart, or when restarts keep cutting it, and it then says what happened.

### The outcome in the conversation

When the item ends, the card the agent posted when it scheduled it updates in place: its state (done, blocked, missed) and the full proof the tool or the agent returned. Below it, one readable line says what happened, where and when, in your time zone, with a link to the run, for example "**Done**: Morning note (Teams chat with Dave Giles), at 07:00 Paris on Mon 9 Nov 2026." An item that did not run gives the reason in one clause and one way to fix it. The line never repeats the raw tool result: chat, message and channel ids stay on the card, in the run and on the **Work Items** page. For an instruction, the agent's own answer follows the line as written. The notification says the same line.

### Missed items

An item is run up to 15 minutes late (for example when the platform was busy). Past that window it is **missed**: it is not run at a time nobody chose, it is marked missed everywhere it appears, and you are notified. Reschedule it to run it, or cancel it.

## Where scheduled items appear

| Where | What you see |
|---|---|
| The conversation | The card, updated in place, then one readable outcome line when it ran (see [The outcome in the conversation](#the-outcome-in-the-conversation)). |
| **Agentic Flow > Schedule** | One-off items next to the assignment triggers, in the same timeline and inventory, marked **One-off** (assignments are **Recurring**). Open it on one agent with the agent's **Coming up** card. |
| **Operations > Work Items** | Scheduled items are ordinary work items with a **Scheduled** marker: the time in your time zone, the tool and target, a content preview, and once run its outcome (done, blocked, missed) with a link to its run. The **Scheduled** filter shows them alone. |
| The agent's **Overview** | A **Coming up** card: your scheduled items on that agent and its next assignment triggers, with Cancel and Reschedule. |
| **Operations > Executions** and the agent's **Activity** | Each run, labelled **Scheduled**, linked to its work item. |

A scheduled item is private to the person who scheduled it, whatever the agent: it runs with their identity and shows their content.

## Manage scheduled items

From the chat, ask the agent to list, cancel or reschedule what it scheduled for you. From the screens above, use **Cancel** or **Reschedule** on the item. Cancelling before the time runs nothing and creates no run. An agent cancels its own one-off with its `cancel_scheduled_work_item` tool, or with `complete_work_item` and the status `cancelled`, and moves it with `reschedule_scheduled_work_item`; both take the item's full id or its first 8 characters or more, as the other tools do. Any other change to the item through the work-item tools is refused, with a message naming these tools. A running item can no longer be cancelled or rescheduled, and an item that ran cannot be moved: schedule a new one.

Scheduled items cannot be claimed, completed or handed to another agent: those are the [work items](work-history.md) tools of the optional `Work Items` built-in tool, which only handle the other work items. An agent that also has a work-item assignment never receives scheduled items, and the scheduler never runs the other work items.

Every scheduled item is recorded in the audit log: who scheduled it and the exact action stored, each cancel and reschedule, each change to its action, and the run that executed it.

## Turn scheduling off

An administrator turns scheduling off for the whole platform in **Settings > Access and security > Security options** (`Scheduled work items`, or `SCHEDULED_WORK_ITEMS_ENABLED=false`). While it is off, every agent's scheduling tools answer that scheduling is disabled by the administrator, and nothing is dispatched; items that come due meanwhile are marked missed. Listing and cancelling keep working. The emergency stop also stops a scheduled run in progress.
