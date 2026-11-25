# Evaluations

An evaluation runs agents, on one or more models, against the test cases of a dataset, and scores every answer. Use it to pick a model for an agent, to check that a new persona, prompt or model does not make the answers worse, or to follow an agent's quality over time. Evaluations and datasets are administrator tools, under `Evaluations`.

This page follows the work in order: build a dataset, create an evaluation, choose how it scores and how its agents run, run it, read the results, then compare and improve.

## How an evaluation works

- A **dataset** holds the test cases. Each **entry** has a prompt for the agent, optionally files, and the **expected answer**: the answer the agent should give.
- The **output family** is the shape every expected answer takes: `STIX Bundle`, `JSON`, `Classification` (a set of labels) or `Prose`. It decides which metrics compare the agent's answer with the expected one.
- An **evaluation** links datasets, agents and models, and sets how to score and how the agents run.
- A **launch** starts the evaluation once. It creates one **run** per agent and model, and each run answers every entry of the linked datasets. An agent that does not call a model runs once per launch, shown as `Model-independent`.

Every run gets three scores, from 0 to 100, kept apart so that one never hides another:

| Score | What it answers |
| --- | --- |
| `Judge Quality` | What an LLM judge thinks of the answers, on five sub-scores. |
| `Overall Quality` | How close the answers are to the expected ones, measured by the output family's metrics, without an LLM. |
| `Overall Performance` | How fast, cheap and reliable the run is, compared with the evaluation's other runs. |

Every setting, score, metric and column of these pages has a tooltip: hover it, or reach it with the keyboard.

In copilot / dev mode, the platform ships a ready-made benchmark: the `OCR benchmark` dataset and evaluation, which score how faithfully a model transcribes six documents. It runs the built-in `Document Transcriber` agent, on that agent's default model until you add the models to compare, and only when you run it.

## Build a dataset

Open `Evaluations`, then `Datasets`, and select `New Dataset`. Give it a name and, optionally, a description and `Default Judge Instructions`: what the LLM judge is told for every entry.

Open the dataset:

- Its `Settings` tab holds the name, description, default judge instructions, the `Read-only` switch and, in the `Output Family` card, the **output family**.
- Its `Entries` tab lists the entries. `Add Entry` opens one with:
    - `Input Prompt`: the message sent to the agent. An entry needs a prompt, a file, or both.
    - `Expected Answer`: the answer the agent should give, in the output family's shape.
    - `Attach File`: files the agent receives with the prompt, as in a chat.
    - `Judge Instructions`: what the LLM judge is told for this entry only.

Two families need more:

- **JSON**: a `JSON Schema` every expected answer must match. An entry can override it. `Seed from Agent` fills the schema with the one an agent declares for its output: check it, then save.
- **Classification**: a `Label Vocabulary`, the closed set of labels every expected answer is drawn from.

Each expected answer is checked against the schema or the vocabulary when you save it.

!!! note "A new dataset has no output family"
    Set one in the dataset's `Settings` tab: an evaluation linked to a dataset without one cannot run or re-score.

The evaluation's `Dataset` tab shows each linked dataset's family. It marks `Needs family` on a dataset that has none, as the dataset list and page do, and `Family conflict` on one whose family differs from the evaluation's, after either was changed: set the same family on both.

### Versions

A dataset saves a new version each time its entries, files, JSON schema or label vocabulary change, and when an evaluation first runs on it. The `Versions` tab lists them; select one to read its entries as they were. A run is scored against the version current at its launch, so editing a dataset never changes past results. A run scored against an older version says so on its page.

### Import and share

- `HuggingFace` imports a dataset from the HuggingFace Hub: enter its id, choose the split and the rows, then map its columns to the prompt, the expected answer and the judge instructions. A dataset identical to one you already have opens that one instead.
- `Import ZIP` and `Export as ZIP` move a dataset, files included, between platforms. A ZIP can reach 500 MB, and a file attached to an entry 40 MB. Behind a reverse proxy, its body-size limit must allow them ([Exposing the platform](../deployment/installation.md#exposing-the-platform)).
- `Read-only` locks a dataset's entries, settings and output family, and prevents deleting it, until you turn it off. Its sharing, set through the API, is not locked.

## Create an evaluation

From `Evaluations`, select `New Evaluation`. The `Create Evaluation` page has five cards:

1. `Settings`: the name, a description and how the agents run (see [Choose how the agents run](#choose-how-the-agents-run)).
2. `Scoring`: the output family, the LLM judge and how the scores are computed (see [Choose how it scores](#choose-how-it-scores)).
3. `Agents`: the agents to evaluate. Mark one as `Baseline`, your reference agent: it shows a star in the run lists, `Compare` and `Search Runs`, and changes no score.
4. `Models`: `Use agent's own model` runs each agent on its configured model; add models to run each agent on them too. A disabled model stays on the evaluation but is skipped until you enable it. The models offered are those of the evaluation owner's providers, also when an administrator edits it. A saved model none of them offers any more is marked `Unavailable`, here and in the `Run` menu, where it cannot be selected: a run that includes it is refused, so remove it or configure a provider that serves it. A disabled model is left out of runs, but stays marked. Copying an evaluation makes you its owner: a model none of your providers offers is kept, marked `Unavailable`, and the copy says so.
5. `Datasets`: link datasets of the evaluation's output family, or with none yet. `New Dataset` creates one here with the output family chosen in `Scoring`. You can link more later from the evaluation's `Dataset` tab.

Select `Create Evaluation`. The evaluation's `Configuration` tab then shows the same settings, plus the judge's model and instructions, `Overall Score Composition`, `Output extractors` and tags: select `Edit configuration` to change them.

## Choose how it scores

### The output family's metrics

The output family decides which metrics compare the answers, without an LLM:

| Family | What it scores | Metrics, for example |
| --- | --- | --- |
| `STIX Bundle` | STIX 2.1 bundles, object by object and relationship by relationship | Entity F1, Relationship F1, Entity F1 Aligned, Relationship F1 Aligned, Entity F1 Soft, Valid STIX 2.1 |
| `JSON` | Any JSON answer, field by field as its schema declares | Field Accuracy, Schema Validity |
| `Classification` | A set of labels against the label vocabulary | Label F1, Exact Label Set, Only Known Labels |
| `Prose` | Free text against a reference answer | Word Overlap F1, Word Sequence Overlap (ROUGE-L) |

For a `STIX Bundle` evaluation, `Entity F1` and `Relationship F1` count an object only when the answer names it exactly as the expected bundle does (case and spacing aside), which is how OpenCTI recognises an object it already holds. The aligned metrics read the answer as an analyst would: each answer object is paired with the one expected object it names, through its aliases, ATT&CK id, spelling variants (`U.S.`, `USA`, `United States`) and close types (an intrusion set and a threat actor of one name), and a relationship counts whichever name of its two ends the answer used. `Entity F1 Soft` also gives part of a match to a close name or a confusable type (malware or tool, sector or organization, country or region, an ATT&CK technique and its sub-technique). The detail of an entry lists what was missed, what was extra, and the fragments of an object the answer split in two.

For a `JSON` evaluation, `Per-Field Scoring` in `Configuration` changes how a field is scored, or how much it counts, where its schema's default does not fit. `Configuration` lists the fields you changed; select `Edit configuration` to change them.

Performance is measured the same way for every family: `End-to-End Latency`, `Time to First Token`, `Model Calls`, `Tool Calls`, `Retries`, `Total Tokens`, `Estimated LLM Cost`, `Completion Rate` and `Tool Error Rate`.

### Overall Score Composition

`Overall Score Composition`, in `Configuration`, chooses which metrics make up `Overall Quality` and `Overall Performance`, each with its own selection. `Use defaults` uses the family's default metrics. To choose them yourself, turn `Use defaults` off, then switch each metric on or off; turning it back on restores the defaults. Every metric is measured and stored either way: saving a new selection recalculates the scores of every run at once, without running the agents again.

### The LLM judge

The LLM judge reads each answer next to the expected one and grades five sub-scores, from 0 to 100: `Tool Selection`, `Completeness`, `Accuracy`, `Instruction Adherence` and `Coherence`. They combine into the entry's judge score. Turned off in `Scoring`, no judge runs and `Judge Quality` reads N/A; the output family's metrics still score.

- `Evaluator Model`, in `Configuration`, is the model the judge runs on. `Platform default` uses the platform's default model.
- `Judge Instructions` tell the judge what to look for. An entry's instructions replace the evaluation's, which replace the dataset's: they are never combined. The evaluation's are set in `Configuration`.
- `Sub-Score Weights` set how much each sub-score counts in the judge score, 20% each by default. They are set when you create the evaluation; `Configuration` shows them but does not change them, since a change would not re-score the runs already scored.
- `Judge Output Budget` is the most tokens the judge may use for its verdict (8192 by default). A verdict cut at that budget counts as a judge failure: raise it if long answers leave verdicts unscored.

### Consistency and comparison

- `Consistency Penalty` lowers the scores of a run that is uneven across entries: each metric's mean is shifted toward the worse side in proportion to its spread, by the factor k (0.5 by default). A higher k favours agents that do well on every entry.
- `Performance Tolerance` sets how close to the best run a run must be to score 100 on a performance metric (5 % by default). The worst run scores 0; the others score in between. `Overall Performance` therefore compares runs within one evaluation, and changes as runs are added.

## Map an agent's answer: output extractors

An agent rarely answers in exactly the shape the evaluation scores: it may wrap a JSON object in text, or put its labels inside a larger object. An output extractor turns its answer into that shape before the output family's metrics score it. It is set per agent, in `Output extractors`:

- **Source**: the agent's answer, or the output or input of the agent's last successful call to a named tool.
- **Operations**, run in order, each on the previous one's result: find the JSON in a text, read a text as JSON, take the value at a path, list an object's keys or values, and others.
- **Output kind**: what the last operation must produce: a label set for `Classification`, an object or an array for `JSON`.

The preview runs the extractor on a stored result or a pasted sample, so you can check it before saving. An agent with no extractor is scored on its raw answer. On a run's page, an entry shows the extracted answer next to the raw one, or why the extraction failed.

## Choose how the agents run

The `Settings` card holds how the agents run during an evaluation.

### Execution mode

- `Full Loop with Tools`, the default, runs the agent as it is configured: its handovers, tools and knowledge bases. Every agent of a handover chain counts in the entry's tokens, cost and calls.
- `LLM Only` sends only the agent's persona and the entry to the model, so it measures the model and the persona alone. Its runs are labeled `LLM Only` wherever they are shown.

In both modes, an agent whose output format is JSON delivers bare JSON, as in a chat. In Full Loop, an entry whose agent run fails is recorded with its error and scored 0.

In both modes, a run measures the model it names: every model call of the evaluated agent goes to that model, and the agents it hands work to in Full Loop run on their own models alone. The failover to another provider or to the fallback model, and the switch to a cheaper model when a budget runs out, keep a chat answering but do not apply to an evaluation run. When the provider refuses the model, for example because its key or gateway policy does not allow it, each entry fails with that error, is left out of the scores, and a run where no entry could be scored ends `Failed`.

### Run mode and timeout

- `Run Mode`: `Parallel`, the default, starts a launch's runs together, a limited number at a time; `Sequential` runs them one after another.
- `Execution Timeout (s)`: how long one launch may take, all its runs together (3600 by default).

### Prediction reuse

With `Prediction Reuse` on, the default, a run copies a stored answer instead of running the agent again when nothing that shapes the answer changed: the agent, model, execution mode, persona, prompt and setting versions, the entry and its files, and the knowledge bases. Only successful answers are copied, and scores are always recomputed. A copied entry is marked `Reused`: its performance metrics are its source run's.

### Prompt guards

`Apply prompt guards` checks each prompt and answer with the platform's prompt guard, as chat does: a blocked prompt is refused and a withheld answer is scored as the withheld message. It is off by default, since each check can cost model calls. Runs that applied it show a `Prompt guard` badge.

### Tool approval

`Require tool approval` is off by default: nobody can approve during a run, so tools run without approval, and the settings list the tools marked "always ask" that will run unapproved. Turned on, a call that needs approval ends the entry, which is scored as it is and shows `Needed approval`; the run shows a `Tool approval` badge.

## Run it

- `Run`, or `Run All` in its menu, launches every agent on every enabled model, and on each agent's default model when the evaluation uses it.
- `Run Selected`, in the same menu, launches only the models you pick under `Run on`. A model the evaluation does not have yet is added to its enabled models.
- To run it on a schedule, turn on `Scheduled Run` in the `Settings` card and enter a cron expression, read in UTC: Daily at 8 AM starts at 08:00 UTC. On a saved evaluation, `Automatic runs` off pauses the schedule without removing it.
- An agent, through the platform tools, or the API can launch it too.

Every launch is checked first: the evaluation and its datasets have an output family, disabled models are left out, and every model is offered by a provider. A launch you start is refused with the reason; a scheduled one is skipped, and its reason written to the platform log.

`Cancel all runs in this batch`, on a group of runs in `Activity`, stops its running and pending runs.

### Export and import

`Export evaluation bundle` saves the evaluation with its agents, datasets and runs to a ZIP file; `Import Evaluation`, on the `Evaluations` page, brings one in. Its runs keep the scores they had. It arrives with automatic runs off, so its schedule never fires until you turn it on. A model none of your providers offers is kept, marked `Unavailable`, and named in the import's warnings. An import that fails creates nothing, so you can correct the file and import it again.

## Read the results

### The Overview

The evaluation's `Overview` ranks its agents and models on each score: the `Leader` of each, its `Top 10`, and charts of each score over time and of each pair of scores against each other. A score's number is coloured by its band: green from 70, orange from 40, red below. The dot beside a score's name only says which score it is.

### Activity

`Activity` lists the runs in groups, newest first, with each run's three scores, versions and status. Select runs to re-run, re-score, compare or delete them.

### A run

A run's page shows its three scores, its duration, the versions it ran with and its parameters, then:

- `Metrics`: every metric the run measured, under `Overall Quality`, `Overall Performance` and `Execution Telemetry`. Each row says whether it counted in the score, what it contributed, and its mean, median, 95th percentile, minimum and maximum over the entries.
- `Dataset Results`: one row per entry, with its judge score and duration. Open an entry to read the agent's answer, the extracted answer, the expected one, the judge's sub-scores and reasoning, its metrics, and the timeline of the agent's steps.

An entry shows the metrics that mean something on a single entry. A `Classification` entry, for example, shows one `Label F1` with its labels found, missed and extra; the run's `Metrics` panel keeps the three ways of averaging it across entries: `(Micro)`, `(Macro)` and `(Samples)`.

### What is left out of the scores

Some entries are measured but left out of the scores, and the run says how many and why:

- **Execution anomalies**: an entry that failed with a transient provider error (an overload, a rate limit, a server error) or that the platform could not run. They say nothing about the agent: re-run them.
  - An agent with `Skip LLM` whose `Prepare tool` or `Finalize tool` refused or returned an error (no input, no licence, an integration without a base URL) is one the platform could not run. The entry shows the tool's message; fix the configuration and run again. Its answer is never reused.
- **Judge failures**: a verdict the judge could not deliver, or cut at its output budget. The entry keeps its other metrics.
- An entry with no value for a counted metric is left out of that metric, and the run lists the reasons.

## Compare and improve

- `Compare`, on two or more runs selected in `Activity`, shows their scores side by side, the parameters that differ, and every entry's answers, judge scores and timelines next to each other.
- **Compare models**: on an evaluation's page, `Compare models` (once two runs have completed) opens three tabs on its completed runs (the 50 newest):
    - `Leaderboard`: the launches (the runs started together, as `Activity` groups them; manual launches and scheduled reruns in two colours), each bar its best run, zoomed on the range reached; pick which launches are columns (`Latest`, `Best`, `Experiments`, `Scheduled reruns`, `All`, or click bars) and their order. Then either each agent and model against the launches, every cell one run's figure on its own entries with its trend, or `Every run, ranked`. Rank by Bundle F1, any family metric, the judge score, the cost or the duration. Click a run to review it; Shift+click a first run to make it the reference, then click the run to compare with it.
    - `Results`: the runs of one launch, every run of one setup (the same agent version, prompts, skills and settings: its reruns), the newest run of each configuration or every run; each agent and model ranked with a 95% interval; its figure against its LLM cost or agent time per entry, with the Pareto frontier (the configurations no cheaper or faster one beats); how far reruns of one setup spread, with the expected objects and relationships found in every run, in some or never; and the methodology to publish next to the figures. `Export CSV` downloads the ranking, `SVG` each chart, `Markdown` the methodology.
    - `Run against run`: one run against another, entry by entry, with the change and its 95% interval (the default model's run, then the baseline agent's, first; pick another pair above the chart), then every agent and model ranked with a 95% interval. On a `STIX Bundle` evaluation, click an entry of the chart to review both answers. Pick other runs in `Runs compared`.
- **Compare evaluations**: on the `Evaluations` page, select two to four evaluations of the same output family and click `Compare`. The comparison works on the entries their datasets share (the same expected answer; for `STIX Bundle`, the same objects and relationships whatever their identifiers) and on one run per agent and model of each evaluation, the newest unless you pick others in `Runs compared`. It shows:
    - `What can be compared`: for the entries, agents, input (text or attached file), models, metric suite, judge and run limits, what each evaluation has and what the difference means. When the judges differ, judge scores are left out; when the metric suites differ, re-score the older runs first.
    - `Same model in both evaluations`: each metric on the shared entries, the change and its 95% interval, cost and time per entry, and the change entry by entry.
    - `Every configuration on the shared entries`: one ranking of every agent and model with 95% intervals. A rank such as `1–4` means the run could be anywhere from first to fourth: with few entries, overlapping intervals mean no single best can be claimed.
    - `Do the shared entries stand for each dataset?`: each run on the shared entries against its other entries.
- **Run review** (`STIX Bundle`): `Review this run` on a run's page, a click on a run in the `Leaderboard`, or `Review bundle` on an entry opens one run over all its entries, against the dataset, nothing, or another run of the evaluation (pick them above, or a quick comparison):
    - the run's figures over its entries (Bundle F1, Entity F1, Relationship F1 with precision and recall, the judge score, cost and time), each with its change against the reference run or the previous run of the same agent and model;
    - `Where the entity points go`: its near-miss pairs by kind (another name or alias, the same ATT&CK id, other identifying properties...), its missing, spurious and duplicate objects; click a cause to see which entries have it;
    - every entry with its figure and change: click one to review it in place, with its score cards, the judge's reasoning, the objects by type and the five views below. An object's history across the runs of the same agent and model is in the panel on the right.

    An entry the run's figures leave out (a platform error or a judge failure) is left out here too; an answer the scorer could not read counts as everything missing.
- **Review a STIX bundle**: on a `STIX Bundle` entry, `Review bundle` (on an entry's line in a run), `Review bundles` (on an entry's line in `Compare`) or an entry of `Compare models` or `Compare evaluations` opens the expected bundle against the answers, object by object as the scorer compares them: by type, on the report text, side by side, as a relationship graph and as a knowledge graph. An object named differently from the expected one (another name or alias, an ATT&CK id, a defanged value) is shown next to it as a near-miss; it still counts as missed and spurious in the score.
- `Re-run full`, on a completed run, and `Replay`, on a failed or cancelled one, start a new run with the same versions and pinned dataset versions. `Re-run full` never reuses answers; `Replay` follows `Prediction Reuse`. The original run is not changed.
- `Re-score` scores a run's stored answers again with the current scoring settings and replaces its scores. The agents do not run again; the judge, when on, calls its model again.
- **Tags** label an evaluation, its agents and its runs. A run's tags merge the three: the run's win over the agent's, which win over the evaluation's.
- `Search Runs`, on the `Evaluations` page, finds runs across every evaluation by a minimum score on any of the three scores, by tag, or baselines only.

## Placebo control

A score says how well an agent does, not why. A placebo control asks whether the agent's persona, its attached Prompts or its skills earn their place. Each agent runs next to a copy of itself where that text is replaced by a neutral text of the same length, delivered the same way. Optionally, it also runs next to a copy where the text is removed (the no-target arm). A text that does no better than its placebo adds length, not guidance.

### Set it up

In the evaluation's `Configuration` tab, open `Placebo control` and select `Set up`.

- **Targets.** `Persona`, `Attached Prompts`, `All skills`, or single skills, one target each. Prompts and skills targets need the `Full Loop with Tools` execution mode: in `LLM Only` the model sees the persona alone. An agent that lacks a target (no persona, no attached Prompt, a skill it is not bound to) gets no arm for it, and the comparison says so.
- **Trials per entry.** Each entry runs this many times in every arm, from 1 to 30 (5 by default).
- **No-target arm.** Also runs the agent with each target removed (on by default).
- **Custom placebo text.** Optional, at most 20,000 characters. Without it, the placebo comes from a neutral corpus shipped with the platform, in the original's language (English or French). Either way, the placebo shares no distinctive word with the names and descriptions it replaces, and its length is matched in the run model's tokens.

Exporting the evaluation carries the configuration. Importing it keeps every target except single skills, whose ids name skills of the instance it came from: the import says which it left out.

### Run it

In the evaluation's `Run` menu, select `Run placebo comparison`. It runs on the models you selected in the menu, or on all of them. Before it starts, the dialog shows:

- the runs, the agent executions (at most 2,000), the judge calls and the probe calls that measure each placebo in the model's tokens;
- an estimate of the tokens, from each agent and model's last measured run;
- the targets left out, its warnings, and every reason the comparison cannot run.

If the evaluation runs tool calls without approval, those tools run once per arm and trial. The dialog lists each agent's tool sources, and the comparison starts only once you switch on `Run them anyway`.

Scheduled runs never run a placebo comparison, and a comparison's runs never reuse an earlier answer.

### Read the results

The evaluation's `Overview` shows a `Placebo comparison` block with one row per agent, model and target:

- **Quality D**: the real agent's Overall Quality points minus its placebo's.
- **Tokens R**: the real agent's tokens divided by its placebo's. Below 1, the real agent is cheaper.
- **95 % intervals**: from 10,000 bootstrap resamples over the dataset entries.
- **Verdict**: `Beats placebo`, `Trade-off`, `Worse than placebo` or `No better than placebo`. It reads the p-values adjusted across the targets of one agent and model (Holm), below 0.05. With fewer than 3 entries it is `Not computable`, and below 10 entries it says to read it with care. A verdict that rests on tokens alone shows the lower bound of the quality difference next to it.
- **Against no target**: the real agent and its placebo, each compared with the no-target arm.
- For a skill target, how often each arm opened the skill (or its placebo) with `load_skill`.

The results come once every run of the comparison has finished. A row whose runs did not all complete has no verdict. Deleting one of its runs cancels the comparison; deleting every run of the evaluation removes it.

In `Activity`, `Search Runs`, the run detail and `Compare`, a comparison's runs carry a `Real`, `Placebo · <target>` or `No target · <target>` tag. They stay out of the leaders, the top 10 and the score charts, which rank configurations. The detail of a placebo run shows the placebo texts, their lengths against the originals, the corpus version, the seed and the language. A comparison's run cannot be re-run or re-scored on its own: run the comparison again.

The platform tools offer the same: `trigger_evaluation` takes a `placebo` option, `get_evaluation` returns the configuration, and `get_placebo_comparison` reads a comparison and its statistics.

## Next step

Build a dataset from the questions your agents answer every day, then create an evaluation on it and run it on two models: the `Overview` tells you which one to keep.
