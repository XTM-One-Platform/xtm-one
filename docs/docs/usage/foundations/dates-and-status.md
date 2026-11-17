# Meaning of dates and status

XTM One shows many timestamps and status labels, and they do not all mean the same thing. The common confusion is between when a record was created or changed in XTM One, when outside data was last refreshed, when a run started or finished, and whether an assistant or connection is healthy right now.

## Two families of dates

**Technical dates** describe what happened inside XTM One — `created`, `updated`, `refreshed`, `synced`, `last heartbeat`. They tell you about platform freshness and maintenance state.

**Operational dates** describe the lifecycle of work — `scheduled for`, `started`, `completed`, `awaiting input since`, `last run`. They tell you what happened in the workflow.

A few worth pinning down: `created`/`updated` are when a record was made or last changed in XTM One; `started`/`completed` are when a run began and finished; `last heartbeat` is when a connected platform last reported health; `synced`/`refreshed` is when imported content was last updated from its source.

## Status labels

Dates read best alongside status. `Enabled`/`disabled` describe whether an object is active for use. `Connected`, `stale`, or `disconnected` describe the health of an external connection. `Running`, `awaiting input`, `completed`, and `failed` describe the state of a run.

## How to read a page

On configuration pages, dates usually describe the object record. On history pages, they describe execution lifecycle. On connection cards, they describe freshness or health. For example, an agent with a recent `updated` date changed its configuration recently — it does not mean it ran recently; and a run with an old `started` time and no completion may be stuck or waiting.

## Next step

Return to the main guide and continue with the `Home Dashboard` chapter.
