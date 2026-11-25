# CTEM signals

A CTEM signal is a change in your exposure: a new vulnerability on something you run, a host that answers on the internet when it should not, a leaked secret, a supplier named in an incident. Signals are where the exposure management loop starts: XTM One checks each signal against your scope and, when it matters, opens a case.

CTEM signals are part of XTM One with an Enterprise Edition license. They are a different feature from the signals of [Platform Health](health.md).

## Where to find signals

Open `CTEM` > `Signals` in the left navigation (the page address is `/ctem/signals`). You also reach it from the [Home dashboard](home-dashboard.md): the signal counters of the exposure loop, the signal sources button and the activity feed each open the page, filtered where it makes sense. A case shows the signal that opened it under `Opened by`.

The top of the page shows a summary strip: the signals of the last 30 days, the unmatched signals, the signals that opened a case, and the trigger paths that are connected, with the logos of their sources.

## Filter the list

- **State**: the buttons above the table filter on the signal's place in the loop, each with its count: `All`, `New`, `Confirmed`, `Unmatched` (the signal does not match an asset in your scope yet), `Discarded`, `Watchlist` and `Case opened`.
- **Trigger path** and **Source**: the filters of the table keep one or more trigger paths (see below) or one or more sources, for example `Shodan`.
- **Search**: the search box looks in the subject of the signal, its trigger path and its source.

Each row shows the subject, the trigger path, the source, and when the signal was first and last seen. Sort on any column; the list opens newest first. Click a row to open the signal's loop trace on the side: what the scope check decided, and the case it opened, if any. The filters and the open signal are kept in the page address, so you can share the link.

Select rows to triage them in bulk (for example move them to the watchlist or discard them) or add their assets to the inventory. A signal that opened a case cannot be selected: the case owns it.

## Lenses

Every signal is seen through one of three lenses:

- **Inside**: what runs in your environment, where, which version and configuration.
- **Outside**: what the internet can reach.
- **World**: advisories, threat intelligence, leaks and suppliers.

Each trigger path has a usual lens; a source may record a signal of a path through another lens.

## Trigger paths

The trigger path says what kind of change the signal is. There are twelve:

| # | Trigger path | Lens | What fires it |
|---|---|---|---|
| 1 | New vulnerability on a running version | World | A new or updated vulnerability that may affect what you run |
| 2 | Relevant new threat or actor | World | A threat actor, campaign or malware relevant to your sector or stack |
| 3 | Code or dependency flaw before production | Inside | A flaw in your code or dependencies, before it reaches production |
| 4 | Proven exploitable to a primary asset | Inside | A simulation proved an attack path to a critical asset |
| 5 | New exposure | Outside | Something you own answers on the internet when it should not |
| 6 | Stale or orphaned asset | Outside | An asset that no longer resolves or has not been seen for days |
| 7 | Attack-surface drift | Outside | A host or port on your attack surface that nobody declared |
| 8 | Mitigation regression | Inside | A security control that used to block an attack no longer does |
| 9 | Control or evidence decay | Inside | Evidence that a control works is older than its re-test delay |
| 10 | Credential or secret leak | World | A secret or an account of yours found in a leak |
| 11 | Third-party or sub-processor event | World | A report or incident naming a supplier or sub-processor you track |
| 12 | Blind spot or coverage gap | Inside | An asset that supports a risk scenario with no evidence at all |

The trigger path is shown on every row of the list and in the loop trace. Paths 5, 6 and 7 come from the attack surface pass (for example Shodan); paths 1 and 2 mostly from OpenCTI; paths 4 and 8 from OpenAEV; paths 9 and 12 from OpenCRQ.

## Signal sources

The `Sources` button at the top right of the page opens the signal sources: for each trigger path, what feeds it and whether that source is connected. A path shows `Not connected` when no integration feeds it, and `Off` when the integration is connected but its scheduled pass does not run; `Turn on` takes you to the setting in `Settings` > `Organization`. `Back to Signals` returns to the list.

## Who sees which signals

- An administrator sees every signal of the organization.
- A member of a group with a CTEM right sees every signal too: the view right reads them, the decide right also triages them and changes the cases. Your administrator sets these rights on the group.
- Anyone else sees only the signals they recorded themselves, through their own calls or their agents.

Signals recorded by the platform's own listeners and passes are visible only with one of the first two.

## Ask an agent about signals

The built-in CTEM agents (the CTEM Assistant and the discovery, prioritization, triage and threat agents) can list signals with the same filters: state, trigger path, lens, source. Any agent you give the CTEM read tools can do the same. For example, ask the CTEM Assistant:

> List the CTEM signals with lens outside and trigger path new exposure, stale asset or attack-surface drift, newest first, with their source and last seen date.

The agent sees what the person it acts for sees: when it answers with fewer signals than you expect, it says that the rest is outside that person's reach.

## Next step

Follow what a signal became in its case: open `CTEM` > `Cases`, or the case named in the signal's loop trace.
