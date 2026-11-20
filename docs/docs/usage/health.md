# Platform Health

When [Platform Health](../administration/health.md) is on, it looks for ways to improve the agents and flows of the platform, tries them on sandbox copies, and files **improvement proposals**. A proposal changes nothing until someone who manages the target approves it.

## Where to find them

- **Agent page > Overview > Improvements**: the proposals on the agent, its skills and its assignments, and the Platform Health cycles on the agent.
- **A flow's page > Improvements** (the compass in its toolbar): the proposals and cycles of the flow.
- Administrators: the **Platform Health** page (`/health`) lists every proposal.

These appear only to the people who may manage the agent or flow: its owner, a person or group it is shared with at the **config** level, and administrators for a company-managed one. Read-only sharing does not show them.

## The Overview tab

The **Overview** tab of the Platform Health page opens first. Every figure on it comes from checks that run on a schedule without any model call, so reading it consumes no agentic quota.

- **Module cards**: one card per module, with its score out of 100, its open findings by severity and the change over the last 7 days. Agents and fleets appear in every deployment; XTM One, OpenCTI, OpenAEV and OpenCRQ appear in XTM One deployments.
- **Score history**: each module's daily score over 7, 30 or 90 days (administrators).
- **Findings**: what the checks found, most severe first, with search and filters by severity, module and category. The switch above the list shows the open, dismissed or resolved findings with their counts. Open a row to read its evidence, its recommendation and a link to the documentation.
- **Dismiss finding**: sets an open finding aside for 7, 30 or 90 days or until it resolves, with a reason. A dismissed finding leaves the score; the reason is recorded in the audit trail. It comes back if it is still failing when the dismissal lapses.
- **Dismiss all** and **Restore all**: act on the selected rows, or on every finding of the list when none is selected, with one reason for a dismissal. **Restore** on a dismissed finding brings it back to the open list at once. Every change is recorded in the audit trail.

Administrators see every module. Anyone else sees the findings of the agents they manage (owner, or shared at the **config** level), without the module cards of the platforms.

## Reading a proposal

Each proposal shows:

- its type (persona, skill, assignment prompt, flow repair, flow optimization, new flow, agent to flow) and its status;
- the measured result of its experiment: a measured gain, no measured change, or **Unmeasured** when it could not be measured, with the reason;
- how many of the experiment's writes were **simulated** rather than replayed from recorded calls;
- its evidence: links to the traces, ratings and runs you may open. Evidence you may not open is counted, not linked.

Approve or reject it from the target's page. An approved change is applied as a new version of the target and watched: if it regresses within the watch window it is rolled back, unless someone edited the target meanwhile, in which case that edit is kept.

## Improve with Platform Health

The **Improve with …** button (it carries the orchestrator's name), on the agent page and on a flow's page, asks Platform Health to investigate that agent or flow now. It is offered to the people who may manage it. Platform Health says so when it cannot start:

- it is already working on this target;
- the target is outside its supervision scope;
- its budget per cycle or per month is reached;
- agentic AI is off on the platform.

What it finds appears under Improvements once its cycle ends.

## The companion

The companion is a small animated pet in the bottom-right corner of the screen that shows what Platform Health found, and what your own work is doing. It is offered to administrators, who are the people who can open the Platform Health page; other users see neither the pet nor its profile card. It is off until you turn it on in **Profile > Profile > Companion**, where you also pick the pet. The Butler, with a silver tray, is the built-in pet and the default one.

| The pet | When |
|---|---|
| Waves | It appears |
| Looks worried | A Platform Health cycle, or one of your runs, failed in the last 30 minutes |
| Checks its watch | One of your runs is waiting for your approval |
| Presents a letter | A proposal waits for a decision you may take |
| Is at work | A Platform Health cycle is running, one of your runs is running, or the chat on screen is answering |
| Stands by | Nothing of the above |

Click the pet to read what it shows: first the open findings, the most severe kind with its count (for example "2 critical findings are open.", or "No open finding."), then what is happening now. From there you open the Platform Health page or hide the pet; you can show it again from your profile. When your system asks for reduced motion, the pet holds still. It reads only what you may already see, without calling any model.
