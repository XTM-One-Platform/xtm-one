# Presentation Design

How to turn a request for "a deck" into a presentation that reads from the back of the room, using the `generate_presentation` tool. The tool description is the contract (every layout, block, type and field); this skill is the craft: how to structure the deck, twenty-six slide compositions that recur in every good deck and every piece of sales collateral, the portrait page that turns the same blocks into a one-pager, twelve deck arcs built from them, the review pass before you hand it over, and how a deck is shown, exported and shared once it exists.

**The person chooses; you make them aware.** What the person or the "New presentation" dialog set wins (the template, the theme, the format, the length). A choice the brief left open (template or none, slides or a portrait page, length, speaker notes, charts or pictures, a call to action) is stated in your first answer with the default you take and the alternatives in one line, so one reply changes it. When the brief is thin, offer once a short menu of what the studio can do (twenty-six compositions, charts and maps, architecture and Mermaid diagrams, the template's assets and logo sets, portrait one-pagers, PPTX and PDF export, comments and sharing). Never add something the person did not ask for without saying so.

## 1. Working method

1. **Brief before slides.** Get (or infer, and state) four things: the audience, the one thing they must remember, the time slot, and the register (board, team, conference, customer). One slide per minute of talk is the budget; a 20-minute slot is ~15-20 slides, not 45.
    - **Format follows the deliverable.** Something *presented* is slides (`16:9`, the default). Something *read* - a product brief, a datasheet, a battlecard, a case study, a partner one-pager - is a portrait page: `aspect_ratio: "A4"` (or `"letter"` for a US audience), one to four pages, see section 4. Never squeeze a one-pager onto a slide or spread a talk over pages.
2. **Outline first, in your head or in one short message.** A deck is `title -> agenda -> (section -> 2-5 content) x N -> takeaways -> closing`. Write the section titles as claims. If a section has one content slide, merge it into a neighbour.
3. **Generate once, complete.** Author every slide, with speaker notes, then call `generate_presentation` once. Do not build a deck slide by slide across several calls.
4. **Refine surgically, in place.** Every later change goes through `edit_presentation` with the same `presentation_id`: one operation per thing that changes (`replace_text` for a word, `replace_image` for a picture, `update_block` for a bullet list, `update_slide` for a title or the notes, `insert_slide` / `delete_slide` / `move_slide` for the structure). A block is addressed by its position on the slide - `"3"`, `"2.3"` (column 2, block 3) on a columns slide, `"footer"` - and on a `dashboard` `"2"` is panel 2: `update_block` there sets its chart or table and the panel's own `title` and `span`, `replace_block` swaps the figure under the same title, `insert_block` / `delete_block` add or remove a panel. A layout-check remedy on a panel's chart (`value_label_fit`, `legend_position`, `label_size`) is one `update_block` on that panel, never the `panels` list re-sent; the tiles are a slide field (`replace_text` for a word in one, `update_slide` with the whole `tiles` row otherwise). The slides you do not name are guaranteed byte-identical, and the result says exactly which slides changed - tell the user that. Read only the slide you change (`get_presentation` with `slides="12"`). To find where a word or phrase appears, or check it is gone, call `search_presentation_text` - it names each slide, block and field and changes nothing; never probe with a `replace_text`. `generate_presentation` with `presentation_id` is for a wholesale rewrite the user asked for.
5. **Template first, and the user chooses it.** A template is the deck's design (palette, fonts, logos, assets); the `theme` is only its colour scheme, `dark` or `light`, limited to what the template supports. When the user named a template, find it with `search_templates` and pass its id as `template_id`; "no template" is `template_id: "none"`. When they named none in a chat, call `generate_presentation` without `template_id`: with several templates in view it returns the list instead of a deck - relay it and ask which one, then call again with the answer. Never pick one for the user, and never present a default as their choice. With a template, do not set `theme`, `transition`, `chrome` or a cover's `hero` unless the user asks: the template's `deck_defaults` carry the brand (and dress the title and closing slides of a new deck), and a theme the template does not support is refused (the result's `Template:` line names the supported ones). The exception is what the template's footer asks the deck for (`get_template` names its placeholders - an event, a classification): when the user gave it, set `chrome.event_label` or the key in `chrome.fields`. Read the template's `guidance` if `get_template` returns one; it overrides this skill where they differ.
6. **Predefined assets before drawn ones.** `get_template` lists the template's assets with a description and a file id each - diagrams, product flows, screenshots, badges, photos, whatever it ships. When the figure the slide needs exists there, place it with an `image` block; draw an `architecture` block (modules, flows, a core, logos from the template) or a Mermaid `diagram` (a flowchart or a sequence) only for a structure no asset shows. Numbers over categories - a trend, a split, a share, a gauge, a figure by country - are a `chart` block, never a picture: it takes the template's palette, the audience gets tooltips, and the PPTX gets an editable chart (a gauge and a `map`, which PowerPoint has no chart type for, and bars read against two axes are placed as a picture - a map's title, caption and legend as text beside it - and the export notes say so). When the template carries logo sets, `get_template(role="partner_logo")` (or `integration_logo`, `customer_logo`, or `query="VENDOR"` for one mark) lists them with their file ids, and a `logo_grid` places several at once (a `logo_wall` when they are grouped by sector or category). People are `people_photo` assets placed by a `profiles` block (3.20). Never a URL, never an invented id.
7. **A deck that exists is copied, never re-made.** A recurring deck - next quarter's review, this month's report, the customer's QBR - starts as `duplicate_presentation` of the last edition under its new title, then `edit_presentation` on the copy's id for what changed (the dates, the numbers, the cover: the copy keeps every slide, note and slide id exactly). A deck shared with the user read-only is changed the same way: duplicate it and edit the copy. A deck regenerated from what you read drifts on every slide. A copy the user no longer wants is removed with `delete_presentation`, only when they ask: it is final (no trash) and works on their own decks only.
8. **People work in the deck too.** A person's manual edits save as they type and collaborators' changes merge live, so pass `expected_revision` (the `revision N` you read) whenever you address slides by number: a deck saved since is refused, not edited at the wrong slide - read the slides you change again (`get_presentation` with `slides`) and re-apply, or address them by id. `get_presentation` also lists the reviewers' open comments with their ids: address the ones the user points at in the edit and say which you handled; close one with `resolve_presentation_comment` only when the user asks, after the save, with a one-line reply saying what changed. Leave a note of your own with `add_presentation_comment` only when asked to comment or to answer a reviewer (`list_presentation_comments` lists the resolved threads too).

## 2. The rules that make a slide

- **Place the body on purpose.** A body that does not fill the slide should not sit packed under the title: choose its placement from the content, slide by slide. `vertical_align: "center"` usually suits a run of blocks, a lone card, a picture, or a table with its takeaway; `vertical_align: "stretch"` (or `equal_height: true`) usually suits side-by-side columns or sibling cards that should share a height; `start` is for a body that already fills the slide. A takeaway callout that concludes the body usually goes as the last block of `blocks`, so it travels with the run; `footer_block` is still fine when the body fills the slide or when pinning the conclusion to the floor reads better. Prefer adding the block that carries the claim over `distribute: true` to fill the room.
- **One idea per slide, and the title states it.** A title is a claim ("Alerts fell 42% after consolidation"), not a topic ("Results"). If the title needs a comma, it is two slides.
- **Titles and legends on one line.** Every slide title, panel title, chart title and chart legend fits on one line at the size it is drawn. Shorten the label to its key words, drop a qualifier the slide title already carries (the period, the unit, the scope), shorten the series names, or widen the panel (a larger `span`, fewer `panel_columns`); a chart outside a panel can take `legend_position: "right"`. A wrap is the layout check's `wrap_title` or `wrap_legend` line.
- **Three to five elements.** A `content` slide carries 3-5 blocks, or 2-3 columns of 1-3 blocks each. More than that is a document, and belongs in the notes or the next slide.
- **Blocks are sized to their content.** A card or a grid is never stretched to the floor to hide the room left: cards in one row share the height of the tallest, a lone card hugs its text. A slide that ends early is placed on purpose (usually `vertical_align: "center"` with its takeaway as the last block, or `stretch` for columns that share a height), given more content or merged with its neighbour - never spacers as padding, never an empty stretched block.
- **The closing callout is the takeaway.** Almost every good content slide ends with ONE `callout`: what the audience should conclude from what is above it. Forty words at most. On a body that does not fill the slide it is usually the last block of `blocks`, placed with the run; `footer_block` pins it to the floor, which suits a slide whose body already fills it or a conclusion that reads better on the floor.
- **A footer only when it adds a fact.** The `footer_block` carries something the body does not: a number, a source, a consequence. Never one that restates the title or the body in other words, and never one that comments on the audience's ask or on the slide itself ("as requested", "here is the overview"); with nothing to add, leave it out and place the body instead (`vertical_align: "center"` or `"stretch"`).
- **A card grid alone is centred.** A body that is one card grid (`profiles`, `personas`, `numbered_cards`, `feature_list`, `metrics_grid`, `pricing_tiers`, `logo_grid`) sits centred between the title and the footer by default; `vertical_align: "center"` centres any run, never two `spacer` blocks around it.
- **Tones carry meaning, never decoration.** `accent` = the one thing that matters; `success` = works / done / improved; `warning` = caution / open risk; `error` = problem / blocker / critical; `info` = neutral context; `muted` = the baseline, the "before", the superseded option. Use the same mapping on every slide of the deck.
- **Numbers before words.** When a number exists, lead with a `metric` or `metrics_grid`; the list explains it. Values fit in 6 characters ("42%", "3.2x", "12 min"), labels in 3 words.
- **Sections every 2-5 slides.** A `section` slide with a `number`, a short `label` ("Part 2") and a claim-title resets attention. A 10-slide deck needs none; a 40-slide deck needs 6-8.
- **The covers set the register.** The `title` and `closing` slides carry a `hero` backdrop (`plain`, `gradient`, `split` - text left, wash or picture right - `spotlight`; under a template whose `get_template` lists them as its own covers, `mosaic` - colour tiles in the corners, clear of the text - or its shapes `cascade` (tiles stepping down from the top-right corner, the logo large top left, the headline low over a rule), `horizon` (a skyline along the bottom) and `pillar` (a column down the right edge)) in a `hero_tone` (`brand` = the template's gradient, or one palette colour), `hero_accent_word: true` draws the title's last line in the accent, a slide's own `theme` lets a light deck open and close dark, and a `title` slide's `icon_strip` - a short row of icons under the headline - is an event cover's decoration, never the message. A brand template dresses the covers of a new deck: change them only when asked, and a wholesale rewrite re-sends each cover with the `hero` fields it carries, or it falls back to `plain`.
- **Speaker notes say, slides show.** Notes on every content slide, 40-120 words, written as what to SAY (the story, the number behind the number, the transition to the next slide), never a copy of the bullets.
- **Balance rules: the body reads finished.** A slide that passes the layout check can still look unfinished: content packed under the title over a bare bottom third, one column ending far above the other, a lone card floating at the top. Place the body by its composition, never leave `vertical_align` at its default by habit:
    - **Two columns of unequal weight** (four KPI tiles beside one short card): `vertical_align: "stretch"` (or `equal_height: true`) so the cards of the row share the tallest one's height, and give the lighter column a second block; columns whose blocks end more than a fifth of the body apart are the layout check's `uneven` line.
    - **Sibling cards** (three callouts, phase cards, a 2 x 2) share their height: `equal_height: true` on one row, `equal_rows: true` when the columns line up as rows.
    - **A lone block** (a card, a picture, a quote-like callout): `vertical_align: "center"`, or the metric, list or visual that carries it; a lone grid (`feature_list`, `metrics_grid`, `list`, `numbered_cards`) sets `fill: true` (its rows spread, capped at a readable gap) or is centred. A lone block packed under the title over a bare body is the check's `floating` line; a `fill` grid is not moved by `vertical_align`, so its remedy is to drop `fill`.
    - **Numbers and a list** in one run: `vertical_align: "center"` with the takeaway callout last, or `distribute: true` when the metrics and the list read better spread over the body; choose by how much room is left.
    - **`start` only when the body is full** (about 60% of the body or more); otherwise one of the placements above, chosen from the composition (`center` or `stretch` in most cases).
    - **Minimum content.** A content slide is 3-5 blocks, or 2-3 columns of 1-3 blocks; a lone callout is never a slide - add the metric, the list or the visual it concludes, or merge it into the neighbour. `takeaways` and `numbered_cards` items are one full sentence each (cards mostly empty mean too few words, or too many cards).
    - **Never a stretch to hide the room.** These placements share a row's height or centre a run; a card is never pulled to the floor, and a spacer is never padding.
- **Inline markup, sparingly.** `**bold**` for the one phrase per block that matters, `[[accent:text]]` for the number or name to remember, `_emphasis_` for a term being defined. Never markdown headings or lists inside a string: use `heading` and `list` blocks.

## 3. Twenty-six slide compositions

Each composition below is a `content` slide (unless noted). Fields not shown are optional; every example passes validation. 3.1-3.13 are the compositions of a talk; 3.14-3.18 are the ones sales collateral is made of (feature matrix, editions, fact sheet, call to action, personas) and they work on a slide and on a portrait page alike; 3.19 is the KPI dashboard a board or a QBR deck is made of; 3.20 is the team slide (founders, speakers, a leadership team); 3.21 and 3.22 are the two a product keynote is built from (the framed screenshot, the challenge and its answer); 3.23-3.26 are the org charts - who reports to whom, how many, and the seats to fill.

### 3.1 Two columns + takeaway (the workhorse)

Roughly half the slides of a good deck. Left: the situation as a `list`; right: what it means as a `callout` or a second `list`; under them, the conclusion as the last block. The two columns are a `columns` block (`equal_height: true`) so the callout can follow them in one run, and the slide sets `vertical_align: "stretch"` so the columns share a height; the next compositions centre their run instead, the other usual placement.

```json
{"layout":"content","title":"Five consoles cost the team a day a week",
 "blocks":[{"type":"columns","equal_height":true,"columns":[[{"type":"heading","content":"Today","level":3},{"type":"list","style":"cross","items":["5 consoles","Manual correlation","Context lost on handover"]}],
                                                  [{"type":"heading","content":"What it costs","level":3},{"type":"metrics_grid","items":[{"value":"8h","label":"per analyst / week"},{"value":"3x","label":"duplicate alerts"}],"columns":2}]]},
           {"type":"callout","tone":"accent","icon":"lightbulb","content":"The tools are not the bottleneck - the seams between them are."}],
 "vertical_align":"stretch",
 "notes":"Ask who recognises the 8 hours. Transition: so we removed the seams."}
```

Variant: `ratio: "wide_left"` when one side is the argument and the other a short verdict; three columns for three parallel options (each column a `heading` + a short `list`).

### 3.2 Numbers slide (KPI wall)

A `metrics_grid` of 3-4 headline numbers, an optional second row of 2 secondary numbers, and one line of context. Tones on the tiles say whether the number is good (`success`), worrying (`error`) or neutral (`accent`). A delta is coloured by its `trend` (up green, down red) unless `delta_tone` names the news: where lower is better - alerts, triage time, costs, loss, churn, burn - a fall is `"delta_tone":"success"`, and its arrow still points down.

```json
{"layout":"content","title":"Q2 in four numbers",
 "blocks":[{"type":"metrics_grid","items":[{"value":"42%","label":"Fewer alerts","delta":"-12 pts","trend":"down","delta_tone":"success","tone":"success"},{"value":"11 min","label":"Median triage","delta":"-9 min","trend":"down","delta_tone":"success","tone":"success"},{"value":"3","label":"Open criticals","tone":"error"},{"value":"98.7%","label":"Coverage","trend":"up"}]},
           {"type":"text","content":"Same team, same budget. The change is the platform.","align":"center","tone":"muted"}],
 "vertical_align":"center",
 "notes":"Say what changed between Q1 and Q2 before reading the tiles."}
```

### 3.3 Before / after (comparison)

A `comparison` block with the old state on the left (`cross` list, muted) and the new state on the right (`check` list, accent or success), then a callout with the delta as the last block, the run centred.

```json
{"layout":"content","title":"From five consoles to one workflow",
 "blocks":[{"type":"comparison","left":{"label":"Before","icon":"layers","blocks":[{"type":"list","style":"cross","items":["Five consoles","Copy-paste correlation","No shared context"]}]},
                                 "right":{"label":"After","icon":"check-circle","tone":"success","blocks":[{"type":"list","style":"check","items":["One console","Automatic correlation","Context travels with the case"]}]}},
           {"type":"callout","tone":"success","content":"Triage time fell from 20 to 11 minutes in six weeks."}],
 "vertical_align":"center"}
```

### 3.4 Diagram + explanation

One `diagram` (Mermaid) carries the structure; a short list or callout says what to look at. Keep diagrams to <= 10 nodes with 1-3 word labels, one direction. `wide_left` with the diagram on the left is the standard.

```json
{"layout":"content","title":"Detections flow into one case, not five queues",
 "columns":[[{"type":"diagram","source":"flowchart LR\n  EDR --> Correlate\n  SIEM --> Correlate\n  Mail --> Correlate\n  Correlate --> Case --> Respond","size":"lg"}],
            [{"type":"list","style":"arrow","items":["Every source lands in **one** correlation step","A case is opened once, enriched continuously","Response acts on the case, never on a raw alert"]}]],
 "ratio":"wide_left","vertical_align":"center"}
```

Mermaid is for two shapes only: a `flowchart LR` pipeline of plain steps and a `sequenceDiagram` (who calls whom). Everything else has a native block drawn in the template's palette: numbers - a share, a trend, a series - are a `chart`, a dated history is a `timeline` block, a plan is the `roadmap` layout, and a platform is the `architecture` block below.

An **architecture, platform or ecosystem figure** - products and systems, what flows between them, the core the rest runs on - is an `architecture` block, never Mermaid: it is drawn in the deck's palette and fonts, carries the template's product logos, routes the arrows cleanly and is interactive on screen. Place 3-8 `nodes` on the grid (`row` / `col`, 1 = top-left; `col_span` for a band across the top), one `shape: "circle"` + `emphasis: "core"` for the hub, `logo_file_id` from the template's assets where the product has a logo (an `icon` otherwise), a `sublabel` of 2-4 words, a `description` sentence for the hover detail, `chips` on a card for its entry points, a palette `color` (`brand_1`-`brand_4`) on a node or zone when the template's product colours tell the products apart. Name every flow as an edge with a 1-3 word `label` (`arrows: "both"` for an exchange, `arrows: "none"` for a plain tie into the core, `dashed` for what is planned), wrap the platform's modules in one `zone` - headed by the platform's own mark when the template ships it (`logo_file_id` on the zone, its `label` then optional and drawn after the mark), and by its capabilities as the zone's own `chips` (up to 6 short pills on the same row, in the top-right corner; `chips_align: "start"` puts them after the mark), which take no cell from the modules - give a logo that belongs inside the zone without a card `frame: false` (a `ghost` still draws a dashed outline), and add a `legend` when two arrow styles coexist. When the picture needs an arrow to leave or enter by a given side - an exchange drawn as two squared loops, one either side of the core - pin it with `source_side` / `target_side`; two arrows between the same two nodes take `lane: 1` / `lane: 2` to run as parallel lines; and context that feeds the whole platform points at the zone itself (`target: "zone:ZONE_ID"`, the zone carrying that `id`), never at an invisible node. Same composition as above: the figure in the wide column, a list or callout beside it saying what to look at; `size: "lg"`. The tool description carries a validating example.

When the template ships the figure (a `diagram` or `screenshot` asset whose description matches - the platform architecture, a product flow, a dashboard capture), the same composition takes an `image` block in the diagram's place: `{"type":"image","file_id":"ASSET_FILE_ID","alt":"...","fit":"contain","size":"lg"}`. A real figure always beats a redrawn approximation of it; pick the variant drawn for the deck's surface (dark or light) when the description offers both.

### 3.5 Table + callout

A `table` of <= 6 rows and <= 4 columns; `tone` on the cells that matter; the conclusion in a callout as the last block, the run centred. If the table is bigger, it is two slides or an appendix. A table that carries the slide alone takes `fill: true` (its body rows spread over the height the slide leaves it, up to a readable maximum - the header row keeps its height) and `size: "lg"` when it has the room (cells, headers and footnote one step up the type scale); `size: "sm"` steps them down for a dense reference table, and `density` still sets the padding.

```json
{"layout":"content","title":"Buy wins on time-to-value, build wins on control",
 "blocks":[{"type":"table","headers":["Criterion","Build","Buy"],"rows":[["Time to value",{"text":"9 months","tone":"error"},{"text":"6 weeks","tone":"success"}],["Control",{"text":"Full","tone":"success"},{"text":"Configurable","tone":"warning"}],["Run cost","2 FTE","Licence"]],"highlight_first_column":true,"size":"lg"},
           {"type":"callout","tone":"accent","title":"Recommendation","content":"Buy now, keep the build option open for the correlation layer."}],
 "vertical_align":"center"}
```

### 3.6 Feature grid (icon boxes)

A `feature_list` of 4-6 items in two columns: icon, a 2-4 word title, a one-line description, `fill: true` so the rows share the slide's height. For capabilities, principles of a product, or the pillars of a plan.

A row that names a country, a partner or a product takes its picture in place of the marker: `logo_file_id` on a `list` or `feature_list` item (an SVG flag, a small logo; `icon` then left out, a feature row's `icon` becomes optional) and `logo_border: true` for a white picture on a light card (the Japan flag). Never a flag emoji: regional-indicator emojis show as two letters on Windows.

```json
{"layout":"content","title":"What the platform gives every analyst",
 "blocks":[{"type":"feature_list","columns":2,"fill":true,"items":[{"icon":"radar","title":"Detection","description":"Every source, one queue"},{"icon":"git-merge","title":"Correlation","description":"Cases, not alerts"},{"icon":"brain","title":"Context","description":"Intel attached automatically"},{"icon":"shield-check","title":"Response","description":"Playbooks with approval gates"}]}],
 "footer_block":{"type":"callout","tone":"warning","icon":"triangle-alert","content":"Detection quality still depends on the sources you connect - the platform does not invent telemetry."}}
```

### 3.7 Roadmap (phase cards)

Use the `roadmap` layout: 3 phases (2-4 allowed), each with a subtitle for the timing, 2-4 items and a `goal`. Tone the finished phase `success`, the current one `accent`, the future ones `muted`.

```json
{"layout":"roadmap","title":"Three phases to full coverage",
 "phases":[{"icon":"flag","title":"Pilot","subtitle":"Q1 - done","items":["3 SOC analysts","EDR + SIEM sources"],"goal":{"label":"Goal","value":"Prove triage time drops"},"tone":"success"},
           {"icon":"rocket","title":"Rollout","subtitle":"Q2 - now","items":["All tiers","Mail and identity sources","On-call playbooks"],"goal":{"label":"Goal","value":"Retire two consoles"},"tone":"accent"},
           {"icon":"globe","title":"Scale","subtitle":"Q3","items":["Partners and subsidiaries","Shared intel"],"goal":{"label":"Goal","value":"One case model for the group"},"tone":"muted"}],
 "notes":"Dwell on Rollout: that is the decision in the room."}
```

For a dated history rather than a plan, a `timeline` block (`style: "events"`) inside a content slide reads better than phase cards; a step's `logo_file_id` puts the partner or investor of that step under it.

### 3.8 Process loop

A `process_flow` of 3-5 steps, `loop: true` when the last step feeds the first (feedback loops, incident cycles, learning loops). Pair it with a short list of what each step produces, or close it with the callout that says what the loop delivers, as the last block of a centred run.

```json
{"layout":"content","title":"Every incident teaches the next detection",
 "blocks":[{"type":"process_flow","loop":true,"steps":[{"icon":"radar","title":"Detect"},{"icon":"search","title":"Triage"},{"icon":"shield-check","title":"Respond"},{"icon":"graduation-cap","title":"Learn"}]},
           {"type":"callout","tone":"accent","icon":"lightbulb","content":"The **Learn** step writes a new rule or tunes an old one - that is what closes the loop."}],
 "vertical_align":"center"}
```

### 3.9 Principles / takeaways cards

Three to five `numbered_cards`, each a short imperative title with a one-line reason. Inside the deck as a content slide; at the end as the `takeaways` layout.

```json
{"layout":"takeaways","title":"Three things to take away",
 "items":[{"title":"Start with the threat","description":"Not with the tool you already own."},{"title":"Cases, not alerts","description":"Correlate first, then let a human look."},{"title":"Measure time to respond","description":"Alert volume is a vanity metric."}],
 "notes":"Read them slowly. Pause after the third."}
```

### 3.10 Callout stack (options / risks / decisions)

Two to four `callout` blocks, each with a distinct tone and a title, when the slide is a set of positions rather than one argument: risks (`warning` / `error`), the decision (`accent`), what is already handled (`success`).

```json
{"layout":"content","title":"Two risks, one decision",
 "blocks":[{"type":"callout","tone":"warning","icon":"clock","title":"Risk - migration window","content":"Six weeks of dual running; on-call load doubles in weeks 2-4."},
           {"type":"callout","tone":"error","icon":"triangle-alert","title":"Risk - detection gap","content":"Legacy rules not ported are blind spots until ported."},
           {"type":"callout","tone":"accent","icon":"check","title":"Decision requested","content":"Approve the Q2 rollout with a two-week freeze on new sources."}],
 "vertical_align":"center"}
```

### 3.11 Quote / evidence slide

One `quote` with variant `statement` and its credit, alone on a `content` slide - it is centred on the slide, no spacers needed. Use it for a customer verbatim, a finding from a report, or the sentence the deck is built around; `figures` carry two or three facts about the customer under the credit, `size: "hero"` a line short enough to shout.

```json
{"layout":"content",
 "blocks":[{"type":"quote","variant":"statement","content":"We stopped counting alerts the day the cases started closing themselves.","author":"SOC lead","organization":"Pilot team, week 6"}],
 "hide_footer":true}
```

### 3.12 Agenda that recurs

An `agenda` slide once after the title; the same slide again before each part with `current` set to the part starting, so the audience always knows where they are. A template whose `agenda` layout does not list `current` (the Filigran Brand Deck) draws every item alike: there the agenda appears once and `current` is never set. A `footer_block` callout can carry the house rule ("Questions at the end", "45 minutes, 10 for discussion").

```json
{"layout":"agenda","items":[{"title":"Where we were","icon":"history"},{"title":"What changed","icon":"git-branch"},{"title":"What is next","icon":"rocket"}],"current":2,
 "footer_block":{"type":"callout","tone":"muted","content":"45 minutes; questions after each part."}}
```

### 3.13 Logo wall (proof / ecosystem)

One `logo_grid` of 6-12 logos from one logo set of the template (`get_template(role="customer_logo")`, `role="partner_logo"` or `role="integration_logo"` for the file ids), 3-4 columns, each mark on its tile, `logo_height` for how large the marks read, `vertical_align: center` so a lone grid does not hug the title; the title states what the wall proves and the footer callout carries the number behind it. Labels (`show_labels: true`) only when the marks are not self-explanatory. Never mix customers and partners on one wall. When the logos fall into groups - customers by sector, integrations by category - the wall is one `logo_wall` instead: a titled card per group (`title`, `icon`, `logos`), 2-6 groups, which fills the body by itself. Several grids on one slide (one per region or segment) share one `tile_height` (canvas px) with the same `columns` and `align: "start"`, so their tiles line up as one table whatever each holds; a round seal then grows to the tile where a wordmark takes its width, and `logo_height` no longer has to be raised for it.

```json
{"layout":"content","title":"Trusted by the teams that run the largest SOCs in Europe",
 "blocks":[{"type":"logo_grid","items":[{"file_id":"PLATFORM_FILE_ID"},{"file_id":"PLATFORM_FILE_ID"},{"file_id":"PLATFORM_FILE_ID"},{"file_id":"PLATFORM_FILE_ID"},{"file_id":"PLATFORM_FILE_ID"},{"file_id":"PLATFORM_FILE_ID"},{"file_id":"PLATFORM_FILE_ID"},{"file_id":"PLATFORM_FILE_ID"}],"columns":4,"logo_height":"md"}],
 "vertical_align":"center",
 "footer_block":{"type":"callout","tone":"accent","content":"[[accent:8 of 10]] pilots became production deployments within a quarter."},
 "notes":"Say who each logo is and what they run; the wall is the proof, the story is yours."}
```

### 3.14 Feature matrix (editions, us vs them)

A `table` whose first column is the capability and whose other columns are the editions or the vendors; yes / no cells are `true` / `false` (drawn as check / cross, never typed as text), a partial answer is a short word ("Basic", "Add-on"). The column being sold carries `tone: "accent"` on its header - that tints the whole column. Small print (release, date, source) goes in `footnote`, never in a row. Up to 8 rows on a slide, 8-10 on a portrait page; a matrix alone on its slide takes `fill: true`, and `size: "sm"` with `density: "compact"` when it runs long.

```json
{"layout":"content","title":"Community runs the platform; Enterprise runs it for you",
 "blocks":[{"type":"table","headers":["Capability","Community",{"text":"Enterprise","icon":"sparkles","tone":"accent"}],"rows":[["Threat intelligence graph",true,true],["Connectors and integrations",true,true],["AI summaries and reports",false,true],["SSO, RBAC and audit trail","Basic",true],["Support SLA","Community","24x7, 1h on critical"]],"highlight_first_column":true,"fill":true,"footnote":"Feature availability as of the current release; Enterprise also unlocks the XTM One agentic assistant."}],
 "footer_block":{"type":"callout","tone":"accent","icon":"arrow-up-right","content":"Same platform, same data: an upgrade is a licence key, not a migration."},
 "notes":"Do not read the matrix. Point at the two rows that decide it for this audience."}
```

### 3.15 Editions and plans (tier cards)

A `pricing_tiers` block of 2-4 cards. Exactly one tier is `highlight: true` (the one being sold), optionally with a `badge` ("Most popular"). `price` is what the collateral prints - "Free", "Per asset", "Contact us" - never a figure the user did not give; `period` carries the unit. Features are 3-5 short lines that DIFFER between tiers; what every tier has goes in the description or the matrix above. `cta` is the button label only; the link, if any, lives on the banner (3.17).

```json
{"layout":"content","title":"Three ways to run it",
 "blocks":[{"type":"pricing_tiers","tiers":[{"name":"Community","price":"Free","period":"forever","icon":"code","description":"The full open-source platform.","features":["Unlimited assets","All connectors","Community support"],"cta":"Deploy from GitHub"},
                                            {"name":"Enterprise","price":"Per asset","period":"annual","icon":"building-2","description":"Everything in Community, plus AI and support.","features":["AI scenarios and reports","XTM One agentic assistant","24x7 support, 1h critical SLA","SaaS or self-hosted"],"cta":"Request a quote","badge":"Most popular","highlight":true},
                                            {"name":"XTM Suite","price":"Bundle","period":"annual","icon":"layers","description":"The whole suite on one contract.","features":["Shared intelligence layer","Exposure to financial risk","One vendor, one contract"],"cta":"Talk to sales","tone":"info"}]}],
 "vertical_align":"center",
 "notes":"Name the unit the price is indexed on before the cards appear, then stay on the highlighted one."}
```

### 3.16 Fact sheet (badges + key / value)

The at-a-glance strip of a brief, a datasheet or a case study: a row of `badges` for what the reader scans first (open source, deployment options, certifications, industry, region), then a framed `key_value` block in two columns for the facts (deployment, integrations, licensing, support; or customer size, sector, products, go-live). Labels 1-2 words, values one line, `tone` only on the value that sells.

```json
{"layout":"content","title":"OpenAEV at a glance",
 "blocks":[{"type":"badges","items":[{"text":"Open source","icon":"code"},{"text":"SaaS or on-premise","icon":"cloud"},{"text":"ISO 27001","icon":"shield-check","tone":"success"},{"text":"SOC 2 Type II","icon":"shield-check","tone":"success"},{"text":"MITRE ATT&CK aligned","icon":"target","tone":"info"}]},
           {"type":"key_value","columns":2,"items":[{"label":"Deployment","value":"Filigran SaaS (EU / US / APAC) or self-hosted","icon":"server"},{"label":"Agents","value":"Windows, Linux, macOS - native and session-less","icon":"monitor"},{"label":"Integrations","value":"CrowdStrike, Defender, SentinelOne, Splunk, Sentinel, Elastic","icon":"plug"},{"label":"Intelligence","value":"OpenCTI native; STIX 2.1 import","icon":"brain"},{"label":"Licensing","value":"Community (Apache 2.0) or Enterprise","icon":"file-text"},{"label":"Support","value":"Community forum; **24x7 with Enterprise**","icon":"life-buoy","tone":"accent"}]}],
 "vertical_align":"center",
 "notes":"On a slide this is the reference the audience photographs; on a page it sits right under the hero banner."}
```

### 3.17 Call to action (banner)

A `banner` is a full-width brand band: the hero header of a portrait page (`variant: "gradient"`, an `eyebrow` such as "Product brief", the product name as title, the promise as subtitle) or the closing call to action of a deck or page (`variant: "solid"` on a page, `gradient` on a slide). One to three `actions`; the first is the filled button unless another says `primary: true`. A `url` is `https://` or `mailto:` and ONLY one the user gave or the template's guidance names - never a guessed link; a label with no url is fine, it is printed. In a talk the band is the slide BEFORE the `closing` slide.

```json
{"layout":"content",
 "blocks":[{"type":"spacer","grow":true},
           {"type":"banner","variant":"gradient","icon":"calendar","eyebrow":"Next step","title":"See it on **your** environment","subtitle":"A 45-minute guided session with a solutions engineer - your assets, your detection stack, your questions.","actions":[{"label":"Book a demo","icon":"arrow-right","primary":true},{"label":"Read the docs","icon":"book-open"}],"size":"lg"},
           {"type":"spacer","grow":true}],
 "hide_footer":true,
 "notes":"Stop talking after the offer. Let the room ask for the demo."}
```

### 3.18 Who it is for (personas)

The "who we serve" slide of a pitch and the audience strip of a brief: a `personas` block of 2-4 cards, one per role the product is bought BY or used BY (CISO, SOC lead, CTI analyst, red team, GRC officer...). Each card is the role (`name`), one line on what they answer for (`tagline`), 2-3 `pains` in their words (the status quo, not our feature list) and 2-3 `gains` that are OUTCOMES for them, not capabilities. The two lists are labelled once for the whole block (`pains_label` / `gains_label`, "Challenges" / "Outcomes" by default). Pains and gains are lists of one-liners, never paragraphs; a card with only gains is a feature card wearing a hat - drop it or find the pain.

```json
{"layout":"content","title":"Built for the people who own exposure",
 "blocks":[{"type":"personas","columns":3,"items":[
   {"name":"CISO","tagline":"Answers for risk to the board","icon":"landmark","pains":["Cannot prove the detection stack works against **today's** threats","Security spend is defended with maturity scores, not evidence"],"gains":["Prevention and detection scores per business unit, over time","Exposure expressed as financial risk the board understands"]},
   {"name":"SOC lead","tagline":"Owns detection coverage","icon":"radar","pains":["Detection rules age out silently","Every purple-team exercise is a project"],"gains":["Continuous validation of every EDR and SIEM rule that matters","Regressions caught the week they appear, not at the next audit"],"tone":"info"},
   {"name":"CTI analyst","tagline":"Turns intelligence into action","icon":"brain","pains":["Reports land in a wiki nobody tests against","No feedback on whether the intel was actionable"],"gains":["Threat actor TTPs become runnable scenarios in one click","Sees which of their reports changed the posture score"],"tone":"success"}]}],
 "vertical_align":"center",
 "notes":"Ask the room which card they are before going on; spend the rest of the pitch on that column."}
```

### 3.19 KPI dashboard (chart panels)

The slide a board deck or a QBR reproduces from the BI tool: a `dashboard` layout, not a `content` slide of stacked charts. A `tiles` row of 3-6 headline metrics (a `sparkline` under the ones with a history; in a row of five or six, a `sublabel` or `delta` past about 20 characters - a comparison such as `Q3 25 EUR 4.5M / Q2 EUR 6.7M` - wraps at the default size, and `"tile_size":"sm"` on the slide keeps it on one line up to about 28 characters; seven or eight tiles step down on their own), then 2-4 `panels` on a three-column grid, each a `chart` or a `table` under its own title; the chart that carries the slide gets `span: 2`. Charts take raw numbers with `categories` on the axis and one `series` per line or bar; `format` says how they read (`currency` compacts 16500000 to EUR 16.5M, `percent` writes 86 as 86%), `target` draws the goal line, `stacked_bar` splits a total, `donut` shows a share, `gauge` one number against its target, `waterfall` a bridge (opening ARR, new logos, expansion, churn, ending ARR: one series of changes, `totals` the opening and the ending, and the totals must add up), `butterfly` two populations rung by rung (contributors and managers per seniority level: exactly two series, the first drawn left of the spine and the second right, every value a positive size (a 0, a rung one side lacks, is drawn with no bar and no value), the rungs top to bottom; `show_totals` writes each side's total under the legend), `map` a figure by country (`categories` ISO 3166-1 alpha-2 codes such as `FR`, `US`, `GB`, one series; `region: "europe"` / `"americas"` / `"apac"` zooms on the countries a slide is about; `legend_style: "classes"` lists up to five flat classes beside it instead of the ramp, and `markers: false` drops the dots over the countries too small to see). A panel's legend sits on its title's row; a stacked chart with 3+ series in a one-column panel reads better with `legend_position: "bottom"` (one line under the axis, the plot taking the room) - `"right"` stacks it beside a `donut`. Grouped bars whose values the layout check reports rotated (two series over 6+ quarters in a half-width column or a one-column panel) keep their `label_size` with `value_label_fit: "stagger"`: every value upright on two alternating rows - at `value_label_size` when the values should read a step under the axis text (20 under `md`). It works on `horizontal` bars and stacks too: two share stacks on one slide keep one value size, a segment too short for its value written past its stack at that size. Six tiles and four panels fit a 16:9 slide without shrinking; more is a second dashboard slide. One sentence in `notes` per panel: what the number means, not what the chart shows.

Two panels that share the grid half and half - a chart beside a table, two charts of one weight - take `"panel_columns": 2` on the slide: the panels sit on two equal columns, where a `span` is 1 or 2 (2 is the full width) and four single panels fill the two rows. A 1/3 + 2/3 split stays on the default three columns with `span: 2`.

Amounts of two magnitudes on one axis (EUR 0.25M beside EUR 2.5M) read as one family with `"unit_scale":"M"` and `decimals`, never a mix of k and M; `y_min` / `y_max` pin an axis when two panels must be read on one scale or a line needs room over the bars. A dashboard edited later is edited panel by panel: its panel N is block `"N"` (section 1, step 4).

A chart outside a dashboard - on a `content` slide, in a column, in a callout - is titled with its own `title`, drawn at the top inside the chart's frame exactly as a panel title is, the legend beside it; never with a caption (that is the note under the plot: the source, the unit) and never by wrapping the chart in a `callout` for a title. Two by two charts on a content slide are two `columns` blocks of two titled charts with `fill: true`, each plot taking its card to the bottom. In a dashboard the panel's `title` is the chart's title, and a chart in a panel carries none of its own.

```json
{"layout":"dashboard","title":"OpenCTI adoption: the installed base keeps compounding",
 "tiles":[{"value":"6,250","label":"Active instances","delta":"+9%","trend":"up","sparkline":[4100,4800,5300,5700,6250]},{"value":"38%","label":"On 6.x","delta":"+11 pts","trend":"up"},{"value":"112","label":"EE trials","delta":"+21","trend":"up","tone":"success"},{"value":"41","label":"Countries"},{"value":"2,900","label":"Slack members","trend":"up"}],
 "panels":[{"title":"Active instances per quarter","span":2,"block":{"type":"chart","kind":"line","categories":["Q1 24","Q2 24","Q3 24","Q4 24","Q1 25","Q2 25","Q3 25","Q4 25"],"series":[{"name":"Instances","values":[3100,3500,3900,4100,4800,5300,5700,6250]}],"target":6000,"goal_label":"FY plan"}},
           {"title":"Installed base by version","block":{"type":"chart","kind":"stacked_bar","legend_position":"bottom","categories":["Q2","Q3","Q4"],"series":[{"name":"6.x","values":[1400,1900,2400]},{"name":"5.12","values":[2600,2500,2500]},{"name":"Older","values":[1300,1300,1350],"tone":"muted"}]}},
           {"title":"EE trials per quarter","block":{"type":"chart","kind":"bar","categories":["Q1","Q2","Q3","Q4"],"series":[{"name":"Trials","values":[64,78,91,112]}],"show_legend":false}}],
 "notes":"Instances: the base grew 52% year on year and crossed the plan in Q4. Version mix: 6.x is now the plurality - say when 5.12 leaves support. Trials: one in three converts, up from one in five."}
```

### 3.20 Team (profiles)

The founders of a pitch, the speakers of an event, a leadership or an account team: one `profiles` block of 2-12 people (a team of nine is ONE block on a 3 x 3 grid, never three stacked blocks; omit `columns` and the grid balances itself, every row as tall as the tallest card). Each is a portrait (`photo_file_id`, a `people_photo` asset from `get_template(role="people_photo")`; the initials show without one), the name, the role as `title`, and 2-3 one-line facts that make the person credible for THIS audience - a previous role, a track record, a credential - each with an `icon` or the `logo_file_id` of the organisation it names. Names, roles and facts are the user's or a source you read, never invented; a fact is one line, never a bio. Alone on the slide it is centred, and the footer callout says why this team.

```json
{"layout":"content","title":"A team that has run this at scale before",
 "blocks":[{"type":"profiles","items":[
   {"name":"CEO_NAME","title":"Chief Executive Officer","photo_file_id":"PEOPLE_PHOTO_FILE_ID","lines":[{"text":"Head of threat intelligence at a national CERT","logo_file_id":"LOGO_FILE_ID"},{"text":"15 years in security operations","icon":"shield-check"}]},
   {"name":"CTO_NAME","title":"Chief Technology Officer","photo_file_id":"PEOPLE_PHOTO_FILE_ID","lines":[{"text":"VP Engineering of a security scale-up","icon":"building-2"},{"text":"Maintainer of a widely used open-source SOC tool","icon":"code"}]},
   {"name":"CPO_NAME","title":"Chief Product Officer","photo_file_id":"PEOPLE_PHOTO_FILE_ID","lines":[{"text":"Built the SOC product line of a large MSSP","icon":"layers"},{"text":"Former incident responder","icon":"radar"}]}]}],
 "vertical_align":"center",
 "footer_block":{"type":"callout","tone":"accent","content":"Between them, **40 years** of running the operations the platform automates."},
 "notes":"Introduce each person by the one fact this audience cares about - the CERT years for a government buyer, the scale-up years for a CTO - never the whole career. Then land the callout: this team has run, by hand, what the platform now automates. Transition: which is why the roadmap starts where it does."}
```

### 3.21 Product showcase (framed screenshot)

The slide a keynote shows the product on: a `screenshot` asset of the template (`get_template(role="screenshot")`) as an `image` with `frame: "browser"` (a window bar with three dots and an address pill; `"browser_slim"` is the same window with a bar a third as tall and no pill, the frame to use when several screenshots share a slide, side by side in columns; `"device"` is a bezel with a camera dot) in the wide column of a `wide_left` slide, and what to look at beside it as a short `list` or a `callout`. The frame sits on a glow in the template's brand gradient and hugs the picture at its ratio - never a letterboxed box - and `fill: true` makes the window the largest the body holds, centred in the column; `vertical_align: "center"` keeps the text level with it. The picture is the real screen, placed by its file id and swapped with `replace_image`; never a window drawn in a browser and placed as a picture.

A product video plays in a `video` block (`{"type": "video", "video_id": ...}` for a Video Studio video, which plays its ready render with its poster and captions; `file_id` for an MP4 or WebM file), with the same `frame` and `fill` as an `image`; `autoplay: true` plays it muted when the slide is shown. Never a GIF placed as an image.

```json
{"layout":"content","title":"Every alert lands in one investigation",
 "columns":[[{"type":"image","file_id":"SCREENSHOT_FILE_ID","alt":"The investigation workspace: timeline, graph and the case on one screen","frame":"browser","fill":true}],
            [{"type":"heading","content":"What you are looking at","level":3},{"type":"list","style":"arrow","items":["The **timeline** of the case, every source merged","The graph of what the indicators touch","The analyst's next step, suggested in context"]}]],
 "ratio":"wide_left","vertical_align":"center",
 "notes":"Point at the timeline first: this is where the five queues used to be. Then the graph - the correlation the team did by hand. Transition: how this changes the day of an analyst."}
```

### 3.22 Challenge and answer

The slide that names a problem and resolves it in one breath: a `challenge_answer` block - the challenge on top in the `error` tone, the answer below in `success`, two cards of one width and one left edge joined by an arrow, each with a small eyebrow (`challenge_label` / `answer_label`, "Challenge" / "Answer" by default) and an `icon` on the challenge for the subject. One sentence per card, in the audience's words; the proof goes in the footer callout or the next slide. Alone on the slide it takes `fill: true` so the two cards share the height; in a column it pairs with a `metric` or a short `list`. Three problems are three slides, or a `comparison` - never three of these stacked.

```json
{"layout":"content","title":"Correlation was the job nobody had time for",
 "blocks":[{"type":"challenge_answer","icon":"layers","fill":true,
   "challenge":"Five consoles, five queues: every incident is correlated **by hand**, and the context is lost at each handover.",
   "answer":"One case model: every source lands in the same investigation, enriched as it goes, and the handover carries the case with it."}],
 "footer_block":{"type":"callout","tone":"accent","content":"Median triage fell from **20 to 11 minutes** in the first six weeks."},
 "notes":"Read the challenge as the room lives it, pause, then the answer. The footer is the proof: say where the number comes from."}
```

### 3.23 Leadership team (org chart)

Reporting lines, teams and headcount are an `org_chart` block, never an architecture block, stacked pills or a drawn picture. Each seat names its `manager` by id and has a `kind` with one meaning across the deck: `person` (a real seat - name, role, a `people_photo` asset as `photo_file_id`, the initials without one), `vacant` (open today, its role only, `period` as its target date), `planned` (a future hire, its `period`), `group` (a team drawn as one card - `role` the team, `count` its headcount, `name` its leader), `bubble` (a team drawn as a circle whose area is its `count`), `external` (a contractor, partner or agency). A secondary manager is `dotted_to`, drawn dashed. The deck lays the tree out itself - equal cards per level, one bus per manager, the leaves folded into a column when a level is too wide - and refuses to shrink it under a readable size: past 30 seats fold the teams (`collapse_below`, `depth_limit`) or draw them as bubbles. Names and roles are the user's, never invented; a role is a title, the rest goes in the seat's `note` (the tooltip). The root wears the core emphasis on its own.

```json
{"layout":"content","title":"The leadership team, and the seat we are filling",
 "blocks":[{"type":"org_chart","fill":true,"nodes":[
   {"id":"ceo","kind":"person","name":"CEO_NAME","role":"Chief Executive Officer","photo_file_id":"PEOPLE_PHOTO_FILE_ID"},
   {"id":"cto","kind":"person","name":"CTO_NAME","role":"Chief Technology Officer","manager":"ceo","chips":["Platform","R&D"]},
   {"id":"cfo","kind":"person","name":"CFO_NAME","role":"Chief Financial Officer","manager":"ceo"},
   {"id":"coo","kind":"person","name":"COO_NAME","role":"Chief Operating Officer","manager":"ceo"},
   {"id":"cro","kind":"vacant","role":"Chief Revenue Officer","period":"Q3","manager":"ceo"},
   {"id":"cpo","kind":"person","name":"CPO_NAME","role":"Chief Product Officer","manager":"cto","dotted_to":["coo"]}]}],
 "notes":"Introduce the team from the top, one line each - the open seat last: say when it will be filled and who covers it meanwhile. The dashed line says the product lead works with operations as closely as with engineering."}
```

### 3.24 Headcount plan (today against the target)

A team's headcount, how it grows and where: the leads as cards over their teams as bubbles (`style: "hybrid"`, each circle's area its `count`), the hires as `planned` seats with their `period`, `show_counts` writing every manager's roll-up computed from the tree, `count_mode: "both"` writing it as today against the target ("40 -> 47"). The roll-up counts a person 1, a team its count, the open and planned seats apart; a team that has reports AND a `count` is held to them - the layout check says so when they do not add up. The title states the plan in one number.

```json
{"layout":"content","title":"Engineering grows from 40 to 47 by H1 FY29",
 "blocks":[{"type":"org_chart","style":"hybrid","show_counts":true,"count_mode":"both","fill":true,"nodes":[
   {"id":"vp","kind":"person","name":"VP_NAME","role":"VP Engineering"},
   {"id":"plat","kind":"person","name":"LEAD_NAME","role":"Platform lead","manager":"vp"},
   {"id":"apps","kind":"person","name":"LEAD_NAME","role":"Apps lead","manager":"vp"},
   {"id":"plat-team","kind":"bubble","role":"Platform","count":9,"manager":"plat"},
   {"id":"plat-hires","kind":"planned","role":"Platform engineers","count":4,"period":"H2 FY28","manager":"plat"},
   {"id":"apps-team","kind":"bubble","role":"Apps","count":22,"manager":"apps"},
   {"id":"apps-hires","kind":"planned","role":"Mobile engineers","count":3,"period":"H1 FY29","manager":"apps"}]}],
 "notes":"Lead with the total: forty today, forty-seven by the end of the half. Then where the seven seats go and when - platform first, mobile once the platform hires are in."}
```

### 3.25 Hiring plan (open and planned seats)

The seats a recruiting plan fills: the managers as `person` cards, every seat open today as `vacant` (the warning tone, "Open role", its target date as `period`), every future seat as `planned` (the accent tone, its `period`; `count` 3 draws one card reading "+3" before its role instead of three), `count_mode: "target"` to write each manager's team once all are filled. Dating every planned seat matters: a level of planned seats with no period is a layout-check advisory.

```json
{"layout":"content","title":"Five seats to fill before the summer",
 "blocks":[{"type":"org_chart","show_counts":true,"count_mode":"target","fill":true,"nodes":[
   {"id":"head","kind":"person","name":"HEAD_NAME","role":"Head of Security Operations"},
   {"id":"det","kind":"person","name":"LEAD_NAME","role":"Detection lead","manager":"head"},
   {"id":"res","kind":"person","name":"LEAD_NAME","role":"Response lead","manager":"head"},
   {"id":"det-open","kind":"vacant","role":"Detection engineer","period":"March","manager":"det"},
   {"id":"det-plan","kind":"planned","role":"Detection engineers","count":2,"period":"Q2","manager":"det"},
   {"id":"res-open","kind":"vacant","role":"Incident responder","period":"April","manager":"res"},
   {"id":"res-mssp","kind":"external","name":"PARTNER_NAME","role":"MSSP night shift","manager":"res"},
   {"id":"res-plan","kind":"planned","role":"Threat hunter","period":"Q2","manager":"res"}]}],
 "notes":"Two seats are open now, three are planned for the second quarter. Say which one is the hardest to hire, and that the night shift stays with the partner until the responders are in."}
```

### 3.26 Territory or pod structure (bubbles by branch)

How a field organisation is cut: the head over the regional leads, each region's pods as `group` seats with their `count` and `chips` for the countries they cover, `group_by_branch` drawing one tinted zone and one colour per region, `orientation: "left_right"` when the pods are many and the slide is wide. A pod drawn as a bubble (`style: "hybrid"`) shows its size at a glance; a pod drawn as a card carries its chips. One seat open in a region is a `vacant` among its pods.

```json
{"layout":"content","title":"Three regions, six pods, one seat open",
 "blocks":[{"type":"org_chart","orientation":"left_right","group_by_branch":true,"show_counts":true,"fill":true,"nodes":[
   {"id":"head","kind":"person","name":"HEAD_NAME","role":"Head of Sales"},
   {"id":"emea","kind":"person","name":"LEAD_NAME","role":"EMEA director","manager":"head"},
   {"id":"amer","kind":"person","name":"LEAD_NAME","role":"Americas director","manager":"head"},
   {"id":"apac","kind":"person","name":"LEAD_NAME","role":"APAC director","manager":"head"},
   {"id":"emea-n","kind":"group","role":"Northern pod","count":6,"chips":["UK","Nordics"],"manager":"emea"},
   {"id":"emea-s","kind":"group","role":"Southern pod","count":5,"chips":["FR","ES","IT"],"manager":"emea"},
   {"id":"amer-e","kind":"group","role":"East pod","count":8,"manager":"amer"},
   {"id":"amer-w","kind":"group","role":"West pod","count":7,"manager":"amer"},
   {"id":"apac-anz","kind":"group","role":"ANZ pod","count":3,"manager":"apac"},
   {"id":"apac-open","kind":"vacant","role":"Japan lead","period":"Q2","manager":"apac"}]}],
 "notes":"Each region in its colour. Americas carries half the headcount; APAC is the region we are building, starting with the Japan seat."}
```

## 4. Portrait pages (A4 / letter)

A portrait page is the same tool, the same layouts and the same blocks with `aspect_ratio: "A4"` (or `"letter"`) on the deck: a 1920px-wide canvas that is taller than wide, so the type scale a slide uses reads as document body text once the page is printed at paper size. One "slide" is one page, and a document runs 1-4 pages; a brief is one page, two at most (page 1 = what it is, page 2 = editions and next step). This is the format of every one-pager in the collateral family: product brief, datasheet, battlecard, case study, partner or integration one-pager. The platform treats it as a document, not a slide show: the chat card and the viewer stack the pages to scroll and zoom, the library lists it as a page, and the export prints it on real A4 / letter paper - so when you talk about it, say "page", "brief" or "one-pager", never "slide" or "deck".

- **The page fills top to bottom.** 4-7 blocks in one flow (no `columns` at page level - use the block's own `columns` field: `feature_list`, `key_value`, `numbered_cards`). A half-empty page is a slide that picked the wrong format; a page that overflows is two pages.
- **Hero first, action last.** Open with a `banner` (3.17, `gradient`, with an `eyebrow`), close with a second `banner` (`solid`) or a `callout` carrying the call to action; the contact line goes in the footer (`chrome.footer_text`, or the template's footer variable in `chrome.fields`), never in a block. Between them, `heading` blocks (level 2) name each section the way a document does - a page has no `title` of its own beyond the hero.
- **Prefer `light`.** Set `theme: "light"` unless the user asks for dark: a page is printed and read at a desk. With a brand template the light palette is applied automatically - when the template supports `light`; a dark-only template refuses it, so tell the user and keep `dark` or offer a template that supports `light`.
- **Density.** Tables carry 8-10 rows, `key_value` two columns, `feature_list` two columns; `density: "compact"` on the page-2 matrix. Numbers still lead: a `metrics_grid` of 3-4 tiles under the hero is the strongest opening a case study has.
- **Notes become the reviewer's brief.** `notes` on a page are what to check before it ships (source of each figure, date of the pricing), not what to say.

The skeleton of page 1 of a product brief (page 2 is 3.14 + 3.15 + a `solid` banner):

```json
{"layout":"content","id":"page-1",
 "blocks":[{"type":"banner","variant":"gradient","icon":"shield-check","eyebrow":"Product brief","title":"OpenAEV - Adversarial Exposure Validation","subtitle":"Continuously validate your defences against the threats that actually target you, and prove it."},
           {"type":"badges","items":[{"text":"Open source","icon":"code"},{"text":"SaaS or on-premise","icon":"cloud"},{"text":"ISO 27001","icon":"shield-check","tone":"success"},{"text":"SOC 2 Type II","icon":"shield-check","tone":"success"}]},
           {"type":"heading","content":"What it does","level":2},
           {"type":"text","content":"OpenAEV runs **safe, repeatable attack simulations** against your real endpoints, mail flow and detection stack, then scores what was _prevented_, what was _detected_ and what went through - so remediation is driven by evidence, not by CVSS scores alone."},
           {"type":"feature_list","columns":2,"items":[{"icon":"swords","title":"Attack simulation","description":"Native injectors for endpoints, email, web and cloud; Caldera and Atomic Red Team included."},{"icon":"radar","title":"Detection validation","description":"Expectations checked against your EDR, SIEM and mail gateway - per technique, per asset."},{"icon":"brain","title":"Threat-informed scenarios","description":"Scenarios generated from OpenCTI intelligence: the actors and TTPs that target your sector."},{"icon":"chart-line","title":"Posture over time","description":"Prevention and detection scores per team, per platform, per campaign - with the drift explained."}]},
           {"type":"heading","content":"At a glance","level":2},
           {"type":"key_value","columns":2,"density":"compact","items":[{"label":"Deployment","value":"Filigran SaaS (EU / US / APAC) or self-hosted","icon":"server"},{"label":"Agents","value":"Windows, Linux, macOS - native, session-less","icon":"monitor"},{"label":"Integrations","value":"CrowdStrike, Defender, SentinelOne, Splunk, Sentinel, Elastic...","icon":"plug"},{"label":"Intelligence","value":"OpenCTI native; STIX 2.1 import","icon":"brain"},{"label":"Licensing","value":"Community (Apache 2.0) or Enterprise Edition","icon":"file-text"},{"label":"Support","value":"Community forum; 24x7 with Enterprise","icon":"life-buoy"}]}],
 "notes":"Check: certification list against the trust page; agent OS list against the current release notes."}
```

## 5. Twelve deck arcs

Built from the compositions above (numbers refer to section 3). The first six are talks; the last six are the collateral family - what a sales, marketing or customer-success request usually means.

| Deck | Slides | Arc |
|---|---|---|
| **All-hands / company update** | 15-25 | `title` (event label, speaker) -> 3.12 agenda -> `section` per topic -> 3.2 numbers -> 3.13 logo wall of the period's new customers and 3.16 `badges` for the awards / certifications won (the PREDEFINED `badge` assets, `logo_grid` for the customers) -> 3.1 for each initiative -> 3.7 roadmap -> 3.9 takeaways -> `closing` |
| **Conference talk** | 25-45 | `title` (event label, `speakers` - up to four, side by side; 3.20 next to introduce them) -> 3.11 the thesis as a quote or a claim -> agenda -> 5-7 `section`s each with 3.4 diagram, 3.1 argument, 3.3 before/after -> 3.8 the loop you leave them with -> 3.9 -> `closing` with contacts |
| **Executive / board briefing** | 8-12 | `title` -> 3.2 numbers -> 3.19 one dashboard per business line (the BI slides, rebuilt as charts) -> 3.5 table of options -> 3.10 risks and the decision -> 3.7 roadmap -> 3.9 (two or three items) -> `closing`; no agenda, sections only if > 10 slides |
| **Customer pitch / discovery** | 12-20 | `title` -> agenda -> `section` their problem -> 3.18 who it is for (let them pick their card) -> 3.1 x2 -> `section` the approach -> 3.22 the challenge and the answer -> 3.4 the platform diagram (the predefined asset) -> 3.21 the product itself (a framed `screenshot` asset) -> 3.6 feature grid -> 3.3 before/after -> 3.2 proof numbers -> 3.13 customer or integration logo wall -> 3.7 how we would start -> 3.20 the team who will run it -> 3.9 -> `closing` |
| **Post-incident review** | 10-15 | `title` -> 3.2 impact numbers -> `timeline` block (`style: events`) of the incident -> 3.4 what happened, as a diagram -> 3.10 root causes (`error`) and fixes (`success`) -> 3.8 what changes in the loop -> 3.9 -> `closing` |
| **Training / workshop** | 20-40 | `title` -> agenda with house rules -> per module: `section`, 3.6 concepts, 3.4 how it works, 3.5 reference table, 3.1 exercise + expected outcome (`footer_block` = the answer) -> 3.9 -> `closing` |
| **Sales deck / master pitch** | 15-25 | `title` (event, speaker) -> agenda -> `section` the threat and the cost of the status quo: 3.2 landscape numbers, 3.18 who carries it (personas), 3.1 -> `section` the platform: 3.4 the architecture (the PREDEFINED `diagram` asset), one 3.6 feature grid per product, 3.13 integration logo wall -> `section` proof: 3.11 customer verbatim, 3.13 customer logo wall, 3.2 outcome numbers -> 3.14 editions matrix -> 3.15 tier cards -> 3.7 how we would start -> 3.9 -> 3.17 call to action -> `closing` with contacts |
| **Product brief / one-pager** | 1-2 pages | `A4` (or `letter`), `light`. Page 1 = the section 4 skeleton: hero `banner`, 3.16 fact sheet, "What it does" `feature_list`; page 2 = 3.18 who it is for (`density: "compact"`), 3.14 feature matrix, 3.15 editions, `solid` closing `banner` |
| **QBR / customer review** | 10-15 | `title` (customer, quarter) -> agenda -> 3.19 adoption and coverage dashboard (tiles with `sparkline`s, a trend line against the `target`; `success` / `error` tones say the trend) -> 3.5 table of what was delivered this quarter (status in toned cells) -> 3.10 open risks (`warning`) and asks (`accent`) -> 3.7 next quarter as a roadmap -> 3.9 (three items) -> `closing`; every number sourced in `notes` |
| **Battlecard** | 1 page or 2-3 slides | 3.16 facts about the competitor (who, where we meet them, their pitch) -> 3.14 matrix us vs them, OUR column `tone: "accent"`, `true` / `false` cells, the source in `footnote` -> 3.10 callout stack "they say" (`warning`) / "we say" (`success`) -> 3.17 call-to-action banner with a `mailto:` to sales engineering when the user gave one. Internal: never a customer logo wall, never a price the user did not give |
| **Case study** | 1 page or 6-8 slides | hero `banner` (customer, sector) -> 3.16 customer facts (industry, size, region, products, go-live) -> 3.3 before / after -> 3.2 outcome numbers -> 3.11 the verbatim quote -> 3.17 call to action; on slides, `section`s replace the `heading` blocks |
| **Partner / integration one-pager** | 1 page | hero `banner` (the two names, what the integration does) -> 3.16 facts (`key_value` = prerequisites, versions, licence) -> 3.4 the flow: the predefined figure as an `image`, an `architecture` block of the two products only when no asset shows it -> 3.6 use-case grid -> 3.17 `solid` banner with where to get it |

## 6. Review pass before you answer

Run this over the spec you are about to send:

- Every title is a claim of <= 8 words; no slide has two ideas.
- Every slide title, panel or chart title and chart legend fits on one line: shorten the label, drop the qualifiers the slide already carries, or widen the panel (`span` / `panel_columns`); no `wrap_title` or `wrap_legend` line left in the layout check.
- No `content` slide has more than 5 blocks, or a column with more than 3; blocks are sized to their content and a slide that ends early is placed on purpose, usually with its takeaway as the last block (never a card or a grid stretched to the floor to hide the room left).
- Body placed on purpose: a content slide whose body does not fill the slide is placed on purpose (`center` or `stretch`, chosen from its composition), never left floating under the title.
- Every content slide has `notes`; none of them repeats the slide text.
- Tones: `error` only on problems, `success` only on things that are done or better; no tone used because it "looks nice".
- Lists: <= 6 items at normal density; no list inside a callout inside a column.
- Numbers: every metric value <= 6 characters, every label <= 3 words.
- Charts: <= 4 series and <= 8 categories per chart, every series with one value per category, `pie` / `donut` / `gauge` / `waterfall` / `map` with one series, a `butterfly` with exactly two and no negative value, a `gauge` with its `target`, a `waterfall`'s `totals` the sum of the steps before them, a `map`'s categories ISO 3166-1 alpha-2 codes, each country once; a dashboard has 3-6 tiles and 2-4 panels (`tile_size: "sm"` when five or six tiles carry long notes), and a chart is never an `image` drawn with code.
- Diagrams: a `flowchart LR` or a `sequenceDiagram`, <= 10 nodes, labels <= 3 words, no `%%{` directives, no `click`.
- Architecture figures: an `architecture` block, 3-8 nodes with unique ids on distinct cells, every edge between two declared nodes or a node and a zone (`zone:ZONE_ID`), labels <= 3 words, at most one `core`, one zone around the platform, logos only from the template's assets (an `icon` otherwise).
- Org charts: who reports to whom is an `org_chart` (3.23-3.26), each seat naming its `manager`, one root (or the `roots` listed), a kind per seat that says what it is, at most 30 seats drawn (fold the rest), every planned seat dated, a team's `count` equal to what its reports add up to. A reporting tree drawn as an `architecture` block (a node over a row, ties with no arrows) is redone as an `org_chart` - the tools and the layout check name the slide.
- Images and logos: only real platform file ids (a template asset from `get_template`, or a file already in the conversation). Never a URL, never a guessed id.
- Predefined figures: where the template ships the diagram or screenshot, the slide places it as an `image`; the `architecture` block and Mermaid are for what no asset shows. A logo wall is one `logo_grid` of 6-12 logos from one logo role of the template, or one `logo_wall` when the logos are grouped (a titled card per group). A packaging or licensing model ("core + modules + capacity = one license") is a `formula` (the last term `variant: result`), and the example setups under it are `config_cards` (rows of logo + status chip, `state: dimmed` for available but not used), never a picture of them. A term named by a product wordmark sets `show_title: false`; the cards take a `heading` ("Example configurations") and each card's `footer` + `footer_value` (label left, value right).
- Showcases: a product screenshot that carries a slide wears `frame: "browser"` or `"device"` (3.21) with `fill: true` in its wide column; a problem and its answer is one `challenge_answer` block (3.22), one sentence per card, never two callouts and a spacer.
- Sections every 2-5 content slides; agenda `current` updated on each recurrence.
- The deck ends with `takeaways` then `closing`, never on a content slide (a sales deck: 3.17 call to action, then `closing`).
- Matrices: yes / no cells are `true` / `false`, never a typed check mark; the column being sold has `tone` on its header; small print is the `footnote`.
- Tiers: 2-4 cards, exactly one `highlight`; prices and feature lines are the user's or the template's, never invented; features differ between tiers.
- Banners: at most two per page (hero + call to action), <= 3 actions, the `url` only when given (`https://` or `mailto:`).
- Personas: 2-4 cards, each with pains AND gains (2-3 one-liners each); pains in the role's words, gains as outcomes for them - a capability list is a `feature_list`, not a persona.
- Profiles: 2-12 people in one block, 2-3 one-line facts each (1-2 past eight people); a name and a title are one line each - shorten them rather than let the layout check report a cut; portraits only from `people_photo` assets; names, roles and facts given, never invented.
- Portrait pages: `aspect_ratio` is `A4` or `letter` and `theme` is `light` unless asked otherwise; each page has 4-7 blocks and fills the page; never `columns` at page level.

If the tool returns a validation error, it names the slide (`slides.3 (layout=content): ...`): fix that slide and resubmit, at most 3 times on one error, then answer with the error the tool still names (on a first generation, the whole spec; on an edit, the same `edit_presentation` operation with the corrected content). Do not regenerate from scratch and do not drop the slide.

A save that validates is then rendered, and the result's `Layout check:` lines name each slide the audience would see go wrong - a body shrunk to fit, text cut off, a label running past its box - with its remedy. Fix each slide it names with `edit_presentation` on that slide (cut content; never shrink the type), save, and only then answer: at most 6 rounds of fixes on one deck in one turn, then answer with the slides and findings still open. An `advisory` line - a body left bare, columns that end apart (`uneven`), a lone block floating under the title (`floating`), a body of several blocks sitting in the top two thirds (`top_heavy`), a card title or a name wrapped to a second line (`wrapped`: shorten it or use fewer columns) - is a suggestion: follow its remedy when it is cheap (a placement, a block that carries the claim), never pad the slide. An `unfilled` line (a slide that asks `vertical_align: stretch`, `distribute` or `fill` and still ends above the floor, or a `fill` table or a map short of its dashboard panel) is not padding to add: it is the renderer missing what the slide asked, so report it and keep the request. After an edit the check lists only the slides you wrote; the others are counted, and left alone.

The layout check measures; it does not look. Before you call a deck done, `capture_presentation_slides` the slides a measure cannot judge - a chart, an architecture figure, a picture, a cover - and those the check named once you fixed them, a few per call (each capture costs context; never the whole deck). The pixels are for your eyes: a capture is not a file for the user.

Balance checklist, on every capture of a content slide (section 2, Balance rules); a slide that fails one is fixed with `edit_presentation` before you answer:

1. **Empty bottom third:** the last block ends above the lower third of the body: the run is not placed (`vertical_align: "center"`, or `"stretch"` for columns that share a height).
2. **Column bottoms apart:** one column ends visibly above the other (more than a fifth of the body).
3. **Floating lone block:** a single card, picture or callout sits under the title with the body bare below it.
4. **Mostly empty cards:** sibling cards of different heights, or cards whose text fills less than half of them.
5. **Thin slide:** fewer than three blocks with no number, list or visual carrying the claim.

## 7. Delivery

- **Show it.** Every result carries the `[[PRESENTATION:ID]]` marker: put it on its own line in your reply, exactly as given - it draws the deck in the chat, the embedded chat of OpenCTI, OpenAEV and OpenCRQ included. After an edit, link the slides that changed (the result gives each its link); to point at one slide, link it the way `get_presentation` shows.
- **A file only when a file is asked for.** The deck is viewed, presented and exported from its own toolbar; `export_presentation` is for "send me the PDF / the PowerPoint": `pdf`, `pptx` (one picture per slide, speaker notes kept) or `pptx_native` (editable text boxes and shapes - say what its export notes place as a picture). Give the returned `[[FILE:...]]` marker verbatim.
- **Access only when asked.** `share_presentation` opens the deck to colleagues with an account - `read_only` to view, present, export, duplicate and comment, `config` to edit - and your reply names who gets or loses access before you call it. People without an account (a customer, a partner) get a public link, `set_presentation_public_link`, and only when the user asks for one: say that anyone with the link can view the deck, give the URL as returned, and turn on `pdf_download` only when asked.
- **Live polls.** A `poll` block asks the audience (choice, quiz with `correct`, word_cloud, rating; 1-5 questions, each with a stable `id`) and draws a short link and a QR code; votes arrive on the slide live. Give the poll its own slide with a short title and nothing else on it; `layout: "all"` shows every question side by side with its live results (a full-slide poll). `presentation_poll` reads the results (`results`, the default) and drives the poll for the owner or an editor (`open`, `next`, `lock`, `reveal`, `reset`); reset deletes the votes, so only when the user asks. A quiz answer is marked per `reveal`: `manual` (the presenter reveals it), `live` (from the start), `after_votes` with `reveal_after_votes: N` (once a question holds N votes, default 10), `after_seconds` (`reveal_after_seconds`, default 20, after a question's first vote) or `never`; with `controls: "none"`, pick `after_votes` or `after_seconds` so the room votes before it sees the answer.
- **Read-only is for reading.** A deck shared with the user read-only can be read, captured, exported, duplicated and commented, not edited: offer a copy (section 1, step 7).

## 8. What not to do

- Do not build a deck with `generate_file` or answer with a markdown outline when a deck was asked for.
- Do not deliver a one-pager as a 16:9 slide or a talk as portrait pages: the format is decided by whether the result is presented or read.
- Do not put HTML in a string, or markdown headings / bullets inside `content` fields.
- Do not draw a chart with the code interpreter and place the picture as an `image`: it ignores the template, has no tooltip and cannot be edited in the PPTX. Numbers over categories are a `chart` block; a page of them is a `dashboard` slide. The same goes for a person's card or a row of logos rendered in a browser: those are `profiles` and `logo_grid` / `logo_wall` blocks, edited one field at a time - and for a screenshot in a window frame, which is the `image` block's own `frame`.
- Do not draw an organisation as boxes on a grid, pills stacked under a heading or a picture: reporting lines, teams and headcount are an `org_chart`, never an architecture block, stacked pills or a drawn picture. It counts the roll-ups itself, exports as editable shapes, and builds level by level in a video.
- Do not invent fields, tones, icon names or layout ids: the contract is the tool description; the validator refuses everything else and says so.
- Do not spread a new deck over several tool calls, and do not create a new deck to fix an existing one.
- Do not re-submit a whole deck to change one word, one bullet or one slide: that is `edit_presentation`, and a re-generated deck never reads the same on the slides the user did not ask about. The same holds inside a slide: one chart of a dashboard is `update_block` on its panel, never the `panels` list re-sent.
- Do not rebuild a deck that exists - last quarter's edition, or one shared read-only - with `generate_presentation`: `duplicate_presentation` copies it exactly.
- Do not close a reviewer's comment, share a deck or turn on a public link the user did not ask for.
