# ROI methodology

How XTM One (and Copilot mode) computes the value of agentic work, where every
input comes from and how to change it. This is the long form of the one-line
methodology every export carries (`roi_report.METHODOLOGY`) and the source of
the product's methodology page (`/value/methodology`, #9836 R7b). The design
is `dev-docs/roi-outcomes.md` (section 12, as built in 12.14); every figure
is computed by one function, `roi_ledger.figures` over `roi_figures.compute`,
which prices a run as `backend/app/services/roi.py` does.

No figure is ever produced by a model: every number is computed from recorded
runs and the catalog below, and reading a figure never starts a run.

## 1. The model

For every unit of work an agent records (a triaged report, an assessed CVE, a
drafted contract review...):

```
Value      = units x (manual minutes - review minutes) x hourly rate
Net value  = value - AI cost
ROI        = net value / AI cost
Payback    = setup cost / net value per month
```

- **Units** come from the runs: an agent, assignment or fleet declares which
  unit its output counts (`dev-docs/roi-outcomes.md` 3.2); built-in agents
  ship a declaration, orchestrators declare none so a fleet's outcome is
  never counted twice (10.3).
- **Manual minutes** are the time a person needs without the agent, with a
  low / likely / high range; the range of every figure is computed from the
  low and high minutes (10.5).
- **Review minutes** are the time a person spends checking the agent's
  output; measured where the run records it, the catalog value otherwise.
- **Hourly rate** is the loaded rate of the role that would do the work.
- **AI cost** is what was spent, where it was spent: the cost of the model
  calls attributed to the scope in the window, the same calls and the same
  amounts LLM Analytics shows for each day. Every call counts once, for the
  person it is attributed to, its agent and that agent's fleet, never shared
  out over other work. Calls of automations, of chat (agent and general chat)
  and every other attributed call (memory, evaluation, background work) all
  count; the last kind is the "Other AI usage" row, cost only.
- **Setup cost** is zero unless declared; payback is "under a month" when
  one month of net value covers it.

## 2. Labels: where each input comes from

Every input carries one label and a source, on screen ("How this is
computed"), in the CSV `assumptions` section and on the last page of the PDF.

| Label | Meaning | Source shown |
|---|---|---|
| Default | Shipped with the catalog | The publication or the reasoning |
| Published estimate | Taken from a public study | The publication |
| Assumption | A reasoned figure, no study yet | The reasoning |
| Measured | Read from the platform (AI cost, recorded review time) | The span measured |
| Customized | Changed by an administrator | Who, when, and the default |

## 3. What is counted

- Three sources make the total, and the "By source" breakdown shows them:
  **Automations** (runs and work items), **Chat (estimated)** (deliverables
  made in a conversation, counted on the low side) and **Other AI usage**
  (cost only). Every breakdown (source, agent, fleet, unit of work, week)
  adds up to the total; what has no agent, fleet or unit sits in its own
  row ("No agent (general chat)", "No fleet", "No unit").
- Each piece of work and each model call counts for one person: the one
  the run acted for, the person a work item works for, the person of the
  conversation. Work with no person (a system task, a deleted run) counts in
  the organization, its agent and its fleet, and in nobody's own figures.
- A **rejected** outcome counts no unit.
- An **unreviewed** run is counted and shown apart; the review rate says how
  much of the figure a person confirmed.
- A unit with **no baseline** counts its AI cost and no time saved.
- The **window** is 7, 30 or 90 days, the quarter or all time; the trend is
  weekly.
- Older history that cannot be tied to a person yet is not presented as
  complete: the figures then read "since <date>", the first day from which
  every piece of work carries its person.

## 4. Who sees what

- **Mine** is what you did, on any agent (yours, shared with you or
  company-managed) and in general chat; never a colleague's work on an agent
  you can see.
- **An agent or a fleet** shows the work of everyone who used it, in
  aggregate, to whoever can open it.
- **The organization** is one figure for everyone who may see it: an
  administrator, and every user while the administrator leaves "Organization
  totals for every user" on (`roi_org_totals_visible`). Agents and fleets you
  cannot open are summed in "Other agents and fleets".
- **Nobody is identifiable.** A user never sees another person's name, email
  or deliverable titles, on screen, in an export or through an agent tool.
  Any group backed by fewer than k people in the period (k = 5 by default,
  an administrator sets it from 2 to 50) is summed with the next smallest
  into an "Other" row, so it cannot be recovered by subtraction; a week of
  fewer than k people is merged with the next; an agent or fleet used by
  fewer than k people shows your own use of it only. Your own figures and
  the organization total are never suppressed. Administrators see every row.
- **By person** (person, department, hours, value, AI cost, net) is for
  administrators only, off by default, turned on in "Value visibility and
  privacy"; every time it is read, the read is recorded in the audit log.
- When an administrator hides cost data, every cost figure (AI cost, net
  value, ROI, payback) reads "Hidden by your administrator", in every view,
  for every reader, on screen and in the exports, never 0.

## 5. How to change a default

| Input | Where | Effect |
|---|---|---|
| Manual and review minutes of a unit | Settings > Units of work | Moves the value of every past and future run of that unit |
| Hourly rate of a role | Settings > Rates | Moves the value of every unit valued at that role |
| An agent's or fleet's declared unit | The agent or fleet, Outcomes | Moves what its runs count from now on |

A customization is a row the seed never writes: a reseed or an upgrade keeps
it, and "Reset to default" removes it. Every change is audited.

## 6. Exports

Every scope exports, Mine included, for whoever may read it, with the same
figures the screen shows (the same rows suppressed, the same costs hidden).

- **CSV** (`GET /api/v1/roi/export?scope=&id=&format=csv&window=`): the
  total, then every row of By source, By agent, By fleet, By unit of work and
  By week (By person for an administrator with it on), each section adding
  up to the total; the "since" line when it applies; then a `methodology`
  line and the `assumptions` section (`assumption, kind, name, value, label,
  source`).
- **PDF** (`format=pdf`): the total, the breakdowns (first 8 rows each, the
  CSV carries the rest), then a last page "How these figures are computed"
  with the formula, the assumptions and their labels.
