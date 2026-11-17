# 🧩 Asset consolidation

Several connectors can describe the same machine, identity or AI target. This page explains how
OpenCRQ decides that two reports are the same thing, which value wins when they disagree, and what
happens to a value someone typed by hand. It is the behaviour behind two questions support gets
often: *why do I see one asset where two tools see two?* and *why did my edit survive — or not —
the last sync?*

## One asset, one row per source

An asset in OpenCRQ is a **canonical row** carrying the values shown in the interface, plus **one
row per connector that reports it**, carrying what that connector said. Nothing is overwritten
in place: a connector only ever updates its own row, and the canonical values are recomputed from
all of them after every sync.

A person's edit is a third layer. Editing a field pins it: the pinned value outranks every rule
below and survives every later sync. Clearing the field removes the pin, and the field goes back to
being computed. Name, description, category, subcategory, criticality, exposure, URL and linked
person all work this way. A **Resync** action drops every one of those pins at once, clears the
hand-typed observation described below, and re-derives the asset from its connectors — except an
asset no live connector observes, which resync leaves untouched: one created by hand has nothing to
derive it from, and one whose connectors were all deleted would only be re-derived from reports
nothing will ever refresh.

## Which value wins

| Field | Rule |
| --- | --- |
| Name, description, URL, linked person | The most recent non-empty value reported by any source |
| Category | The most authoritative source's value; sources reporting at the same instant are broken by rank, not by luck |
| Subcategory | Taken from the same report the category came from, and dropped if a hand-set category no longer contains it |
| Criticality, exposure | The most severe value any source reports — a machine one tool calls internet-facing is internet-facing |
| First seen | The earliest date any source reports |
| Last seen | The latest date any source reports |

Criticality and exposure take the most severe reading rather than the most recent on purpose: a
source that has not looked recently should not be able to talk the platform down from a risk another
source is still reporting.

### An asset nobody named

A source that reports a blank name reports none: an empty or whitespace name is stored as no name at
all. An asset left with no name, from any source and from a person, is shown under a **name derived
from its properties**, in a muted style so it is never mistaken for a real one, and is sorted and
searched under that name too. The derived name is only a display fallback: editing the asset never
pins it as the name, and a real name, reported or typed, replaces it at once.

| Properties | Derived name |
| --- | --- |
| Machine | The hostname, else the hardware (vendor and model) and the address (IP, else MAC) joined by `·`, e.g. `Cisco Meraki MS220-48 · 10.165.0.3` |
| Identity | The user principal name, else `domain\account`, else the directory object id, else a SID |
| AI target | The model, else the endpoint URL |

An asset with neither a name nor any of these shows "Unnamed asset". Several assets may share one
derived name (the same address, for instance); the asset's page tells them apart.

## A hand-typed observation

The Network, Cloud and AI target connection cards are filled from the family property block each
source carries, merged field by field so one connector's silence never blanks a field another
reported. Filling one of those cards by hand — at creation or from the edit form — writes one more
row shaped exactly like a connector's own, except that nothing produced it: no connector, and an
`origin` of "user" rather than "connector".

That row outranks every connector when the property blocks are merged, so a field typed by hand wins
over the same field a connector reports — the property-block equivalent of the pin above. It carries
no first-seen or last-seen date: those describe an observation, and a person typing a value was not
"seen" at an instant.

What a hand-typed observation never does is decide a scalar. Name, category, subcategory,
criticality, exposure, description, URL and linked person are resolved purely from the pinned values
above; a hand-typed observation is set aside before any of those rules run. A scalar becomes durable
by being pinned, not by being "observed".

### Changing the category keeps what was typed

Moving an asset to a category of another family — a host reclassified as an identity or an AI
target — erases nothing. What was typed for the old family is kept, but it is neither shown nor
used: the cards and the edit form only show the fields of the asset's current family, and the
matcher only reads observations whose family is the asset's current family (see below). Moving the
asset back brings those fields back as they were, so a wrong category pick costs nothing. The same
holds for a connector's own report: a host someone reclassified as an identity no longer shows its
Network and Machine cards, and no other connector's machine report is fused onto it. Only **Resync** clears the
hand-typed values for good, and it is offered only on an asset a connector observes: on an asset
typed entirely by hand, the block of a family it left stays on file for as long as the asset does.

### Why a dedicated `origin`, not "no connector attached"

A source row loses its connector id when that connector is deleted, so an absent connector id cannot
tell a hand-typed row from a connector's own observation whose connector no longer exists — the same
ambiguity the platform already avoids on evidence history, where a missing author means a
connector's own verdict or a state derived from a finding's sources, while a reset keeps the author who requested it and is told apart by its `origin`. `origin` records "connector" or "user" directly instead of
leaving the question to a column that means two different things depending on why it is empty.

## How two reports become one asset

Assets are grouped into **identity families** — machines, identities, applications, AI targets,
security platforms (detection and prevention tooling such as EDR, SIEM or email security,
inventoried but never fused with a host), and an unclassified bucket for what no connector could
place. A family has its own set of properties and its own matching rules, and **matching only ever
happens inside a family**. A hostname shared between a server and an AI target is a coincidence, not
an identity. The family an existing asset is matched in is the one of its current category: an
observation reporting another family is left out.

Within a family, reports are compared in descending order of strength. The first rule that matches
wins, and anything no rule joins stays separate. There is no confidence score, no threshold and no
"possible duplicates" queue to triage: the platform either knows two reports are the same asset or
treats them as two.

For machines, in order:

1. **The EDR machine id.** The strongest, because it is one vendor's own identifier for one device.
   A simulation platform reporting an agent qualifies here only when that agent was produced by the
   matching EDR — the same field can carry another vendor's reference.
2. **A hardware, directory or cloud identifier** — hardware UUID, directory device id, cloud VM id,
   cloud resource id, in that order. Each is its own space and they are never conflated. The order
   matters: a hardware UUID outlives a re-enrollment that a cloud id does not.
3. **A shared MAC address.**
4. **A fully-qualified hostname, agreeing platform, and a shared IP or MAC.** The weakest rule, and
   the only one needing three signals at once.

Every family that has a matcher (machines, identities and AI targets) also matches on the **external
reference** — the id the organisation's own inventory gave the asset — when a source reports one.
Application, security platform and unclassified assets have no matcher, so they are never matched or
consolidated, on external reference or on anything else.

AI targets have a single rule: the same normalized endpoint URL **and** the same model, both
present and both exactly equal. Normalization lower-cases the scheme and host, drops a default port
and a trailing slash, and sorts query parameters, so two URLs differing only in those ways still
match. Nothing weaker beyond that, deliberately. Only one connector reports AI targets today, so no
looser rule has been validated against reality yet, and matching on a name alone would merge a
staging copilot with its production twin.

### Why a weaker match can still be refused

A match at a weak rule is rejected when both sides carry a stronger identifier and those two
disagree. Two machines sharing a MAC — a docking station, a switch, a recycled network card — are
kept apart when their EDR machine ids say they are different devices, and a MAC match is also
refused when both hostnames say "not me".

This is why consolidation can look asymmetric: adding a connector that reports a strong identifier
can *stop* two assets from merging that used to merge on a weak signal alone. That is the intended
direction — the platform prefers two honest assets to one wrong one.

### What this means for a connector

The more identifiers a connector reports, the better consolidation gets for everyone, including
connectors that report fewer. A connector reporting only a hostname can still be joined to a rich
one, because the rich one carries the stronger identifiers the vetoes need.

### A hand-created asset can be adopted

A hand-typed observation (above) is one more report to the matcher, on equal footing with a
connector's: an asset created by hand and later reported by a connector becomes one asset instead of
two, the same way two connectors would consolidate.

Creating an asset pins the values it was created with, exactly as an edit does, so the name someone
typed survives the adoption. An exposure or criticality left at *Unknown* is the exception: it is the
form's default rather than a statement, so it is not pinned, and the adopting connector's own reading
fills it. Choosing *Unknown* on an edit is a statement, and pins it like any other value.

The identifiers a connector alone produces — the EDR machine id, hardware UUID, directory device id,
cloud VM and resource ids, the customer's own external reference — are never shown and never
editable; nobody types those by hand. A hand-created machine can therefore only be adopted at one of
the two weaker rules, a shared MAC address or a fully-qualified hostname with agreeing platform and a
shared IP or MAC; a hand-created AI target is adopted at its own single rule, a typed endpoint and
model matching a connector's.

That has a consequence worth knowing before it surprises someone: the hostname veto above reads a
typed hostname exactly as it reads a connector's. A machine typed in with a hostname that disagrees
with the one a connector later reports is *refused* the MAC match it would otherwise get — the veto
cannot tell "someone mistyped it" from "these are two different machines", so a host created with a
hostname that turns out to differ from its connector's own is never adopted on that basis.

## Tags

A tag's identity is its **name within the tenant**: one canonical tag per name, whoever reported it.
That is what makes filtering on a tag return everything carrying it, rather than one variant per
source. Each origin that reports the tag — a connector, or a person tagging by hand — keeps its own
record of the id it uses for that tag, and the colour it reports.

Matching on that id is what makes a rename at the source follow the existing tag instead of creating
a duplicate. Two consequences are worth knowing before they surprise someone:

- When a renamed tag had **several** origins, it **splits**. The renaming origin moves to the tag
  bearing the new name and the others keep the old one, because renaming the shared tag would
  silently rename it for everyone. Each asset follows the origin that tagged it, since every link
  records which source asserted it.
- Renaming onto a name another origin already uses **merges** the two into one tag.

Removing a connector leaves its tags and its links in place; they simply lose their attribution.

A tag someone adds by hand is exempt from **Resync**, unlike every pinned field and the hand-typed
observation above: it is a business enrichment — "PCI scope", "to be decommissioned" — not a
correction of something a connector reported, so handing the asset back to its connectors leaves it
tagged.

## Where the values come from

Which connector reports what is documented per connector:
[OpenAEV](openaev-connector.md), [OpenCTI](opencti-connector.md).
