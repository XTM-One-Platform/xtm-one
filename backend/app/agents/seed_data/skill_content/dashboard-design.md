# Dashboard Design

How to build a custom dashboard with the Dashboard Studio tools that shows live data and holds up when the person comes back tomorrow. The tool descriptions are the contract (every source type, visualization type and operation); this skill is the craft: the working method, where the data comes from, how to write a platform widget right the first time, and the check before handing over.

**The person chooses; you make them aware.** A dashboard can draw many things from many places. Show what is possible and let the person decide:
- **What the person or the "Create a dashboard" dialog set wins**: the brief, the name, the data they named.
- **A choice left open is proposed, not taken silently**: the plan (step 2) states the default you take for each open choice (which platform or integration, the period, how fresh the data must be, the layout), with the alternatives in one line, so one reply changes it.
- **Nothing the person did not ask for is added without saying so**: an extra widget, an input, a tab.

## 1. Working method

1. **Read the brief.** Who looks at it, how often, what decision it serves, the one figure they must not miss. A dashboard answers questions: write them down, one per widget.
2. **Plan before building.** One short message: each widget with its question, where its data comes from, how it is drawn and how fresh it is. Under it, one line per open choice: the default you take and the alternatives. Then build without waiting, unless the brief left open a choice that changes the whole dashboard (which platform, which period).
3. **Find the data, never invent it.**
   - Platform widgets (live, read with each viewer's access): `list_platform_integrations` for the integration, `describe_platform_widget` for the widget types and one type's example, `lookup_platform_values` for the entity types, filter keys, attributes and values. Never guess a key, a type or a value.
   - Figures that need reasoning or several tools (a summary of the week, a count across integrations): an assignment whose answer is JSON, read by an `assignment` source at a `path`; or an `ask` source, asked when the viewer presses its button.
   - Any other integration (a ticketing tool, an EDR, a SIEM, a spreadsheet): an `integration` source calls one of its tools that only reads, with fixed arguments, and draws its JSON answer at a `path`. Try the tool and its arguments with `preview_source` first.
   - Data the person or an agent keeps in a knowledge base (a table in JSON, JSON lines, CSV or Markdown, the whole document or one heading section): a `knowledge` source. When figures must be consolidated from several places over time, an assignment writes that table into a knowledge base document and the dashboard reads it.
   - Values the person gave, and samples while the real source is being built: a `static` source, said as such.
   - The same data drawn twice: one source, two visualizations (`source_id`).
4. **Read each source before saving it.** `preview_source` returns its shape and a sample as the person will see it, or why it reads nothing and what to do. Fix it until it reads data, at most 3 previews of one source: after the third, say what the source still answers and move on.
5. **Decide how fresh it is, and say it.** By default a source is read by each viewer's page, with their own access. On the server, whoever has the page open, it is read `server` every `refresh` (5 minutes at least), on a `schedule` (a `cron`, UTC), on an `event` (a platform event, or an event an integration raises: at most once a minute however many arrive), or `manual`ly when someone presses Refresh now. Read on the server, it is read with the access of the person who sets it and shown to every viewer, even those who could not read the source themselves. Propose it in the plan, never set it silently.
   - **Alerts and what changed.** A source read on the server can hold `alerts`: thresholds on its figure (a number's value, a series' last point, a breakdown's total, a table's row count) that tell the dashboard's owner and the source's publisher, in their notifications, when the figure starts to cross them - once, not at every read. Each read also says what changed since the previous one (the figure before and after, the items of a breakdown that moved the most), computed from the two reads: never write a "what changed" text yourself, the widget says it from the data. Offer an alert where the brief names a limit ("tell me when there are more than 5 critical findings").
   - **Data that needs an agent** (gathered from several tools, consolidated, summarized): `create_feed` makes an assignment on an agent that has those tools, on a schedule or an event, answering the JSON the widget draws, runs it once and adds the widget; `history` draws a number's trend over its last runs, `knowledge` also keeps the consolidated table in a knowledge base document. Data one read tool gives needs no agent: an `integration` source refreshed on the server.
6. **Pick the visualization from the data's shape.** `dashboard_catalog` lists the types each shape can be drawn with (`shape=...`), and one type's options (`type=...`). A number is a single value or a gauge; a series a line, area or column chart; a distribution a bar chart, a donut or a treemap; rows a table or a list. Prefer the platform's own look for its widgets (the type `describe_platform_widget` names as its view).
7. **Add each widget in one step.** `add_widget` saves the source, the visualization and its place together and says what the widget reads. The person sees it land on the stage. Place the headline figures first and on top, the detail below; half widths side by side, full widths for tables and timelines.
8. **Check before handing over.** `check_dashboard` reads every widget's data: fix each one that is empty, waiting on an input or in error (its line gives the remedy), at most 3 rounds of fixes on one dashboard in one turn: after the third, tell the person, widget by widget, what is still open. Then `check_dashboard` with `capture: true` and look at the picture: a legend cut off, a chart too small to read, an empty space. Never say the dashboard is done without having looked at it.

## 2. Platform widgets

- Start from the example `describe_platform_widget` gives for the type, then change only what the brief needs: the types counted, the filters, the attribute broken down, the number of results, the title.
- A filter's key and values come from `lookup_platform_values` (`filter_keys`, then `values` with a `search`); an entity a filter names is looked up by name (`entities`), never written from memory.
- A widget the viewer narrows (a sector, a simulation) reads an input's `$token$` in its filter value; add the entity input once, above the tabs.
- The period: the widget's own `range` (`7d`, `30d`, `3m`...) when the brief fixes one, else the page's time range input.

## 3. Hand-over

Say what the dashboard shows, widget by widget in one line each, where each figure comes from and how fresh it is, and what you did not do (a source that reads nothing yet, an assignment that has not run). Offer what would make it better next: an assignment that keeps a figure current, an input to narrow it, a published link.
