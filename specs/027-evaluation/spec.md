# Feature Specification: Evaluation

> **Frozen design record of the 1780 delivery (2026-10-30). Not maintained**: the code has moved on since. The current contract is `dev-docs/reference/evaluations.md`; the design is `dev-docs/evaluations.md`.

**Feature Branch**: `1780-featevaluation-evaluation-pipeline-v1-on-xtm-one-without-mlflow`

**Created**: 2026-10-08

**Status**: Implemented

**Input**: One specification of the evaluation vertical as it behaves today. A defect is listed under Known gaps, never written as a requirement.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Prepare a dataset (Priority: P1)

An administrator builds a dataset of entries, each an input and its expected answer, and declares the family of that answer. Every change produces a frozen version, so a run always scores against the entries it started with.

**Why this priority**: Nothing can be evaluated without ground truth in the shape the scorers expect.

**Independent Test**: Create a CLASSIFICATION dataset with a vocabulary and a STRUCTURED_JSON dataset with an answer schema, add valid and invalid entries, edit one after a run, and check the refusals and the pinned version.

**Acceptance Scenarios**:

1. **Given** a CLASSIFICATION dataset whose vocabulary is `phishing, malware`, **When** the administrator adds an entry answered `phishing, spam`, **Then** the entry is refused and the message names `spam`.
2. **Given** a STRUCTURED_JSON dataset with an answer schema, **When** an entry's expected answer breaks the schema, **Then** the entry is refused and the message names the location in the answer.
3. **Given** a run that pinned version 3 of a dataset, **When** the administrator edits an entry, **Then** the dataset moves to version 4, the run still shows the version 3 entry, and the next run pins version 4.
4. **Given** a STIX_BUNDLE evaluation, **When** the administrator links a CLASSIFICATION dataset, **Then** the link is refused.

---

### User Story 2 - Create and configure an evaluation (Priority: P1)

An administrator sets which agents and models run, on which datasets and family, with which judge, scoring and run settings. The configuration decides what every later run measures.

**Why this priority**: Every run, score and comparison depends on it.

**Independent Test**: Create an evaluation with two agents, an extra model, a STRUCTURED_JSON family, the judge off and an extractor; reopen it and check every setting, then copy and delete it.

**Acceptance Scenarios**:

1. **Given** an evaluation using each agent's own model plus one extra model, **When** the administrator disables the extra model, **Then** it stays listed with its history and the next manual run uses only the agents' own models.
2. **Given** a STRUCTURED_JSON evaluation where one agent's extractor produces an object, **When** the administrator changes the family to PROSE, **Then** the save is refused and names the agent.
3. **Given** a user who is not an administrator, **When** they open any evaluation or dataset page, **Then** access is refused.

---

### User Story 3 - Run an evaluation (Priority: P1)

An administrator starts an evaluation by hand, on a schedule or through a callback, and each agent runs with each of its models over the pinned dataset versions. Each run records what it ran with and always reaches a terminal state.

**Why this priority**: Without runs there is nothing to score, compare or reuse.

**Independent Test**: Run an evaluation with two LLM agents, two models and one non-LLM agent: five runs start, show progress, end completed and record their versions.

**Acceptance Scenarios**:

1. **Given** agents A and B, their own model on and one enabled extra model, **When** the administrator clicks Run, **Then** four runs are created, A and B on each model.
2. **Given** an evaluation or a linked dataset with no family, **When** the administrator runs it, **Then** the launch is refused with the reason and no run is created.
3. **Given** a running run, **When** the administrator cancels it, **Then** it ends cancelled, no further entry starts, and the recorded results stay.
4. **Given** a worker that dies during a run, **When** the launch's next attempt starts, **Then** that run is failed with a message to re-run it and is not executed again.
5. **Given** a completed run, **When** the administrator clicks "Re-run full" on its page, **Then** a new run starts with the original agent, model and versions, and reuses no earlier answer.

---

### User Story 4 - Score an entry's quality (Priority: P1)

Every entry gets deterministic quality metrics, computed from the extracted answer against the expected answer, and an optional LLM judge score, each with a status that says whether it was measured.

**Why this priority**: Quality is what an evaluation exists to measure.

**Independent Test**: Run one entry per family with a correct, a wrong and an ill-formed answer, and check each metric's value and status and the judge verdict.

**Acceptance Scenarios**:

1. **Given** a STRUCTURED_JSON entry whose answer is not valid JSON, **When** it is scored, **Then** `json.field_accuracy` is 0 with the parse diagnostic.
2. **Given** an extractor whose path matches nothing, **When** the entry is scored, **Then** it shows an extraction failure with its reason, its quality metrics score 0 with that reason, and the run's extraction-failure count goes up.
3. **Given** a STRUCTURED_JSON dataset with no answer schema, **When** an entry is scored, **Then** `json.field_accuracy` is unavailable and says to set the schema.
4. **Given** a truncated judge verdict, **When** it is read, **Then** the result is a judge failure, its reasoning says why, and it is left out of Judge Quality.

---

### User Story 5 - Measure an entry's performance (Priority: P1)

Every entry gets how long the agent took, how much work and money it spent, and how reliably its calls succeeded, including work done by agents it handed over to or delegated to.

**Why this priority**: Overall Performance and every cost comparison rest on these measurements.

**Independent Test**: Run one Full Loop entry that delegates a task and one LLM Only entry, and check the nine metrics, their units and statuses.

**Acceptance Scenarios**:

1. **Given** a Full Loop agent that delegates a task, **When** the entry is measured, **Then** its tokens, cost, calls and retries include the delegated task's.
2. **Given** an entry served by a model with no price, **When** it is measured, **Then** `performance.cost.llm` is unavailable with the reason, never 0.
3. **Given** two entries with 1 failed tool call out of 1 and 4 out of 10, **When** the run is rolled up, **Then** `performance.reliability.tool_error_rate` is 5/11.
4. **Given** a delegated task still running when the entry's wait ends, **When** the entry is measured, **Then** tokens, cost, calls, retries and both rates are unavailable as "chain incomplete", and latency is still measured.

---

### User Story 6 - Read a run's headlines (Priority: P1)

An administrator reads three separate headlines per run, Judge Quality, Overall Quality and Overall Performance, and sees which metrics counted, what each contributed, and why a metric or a headline is missing.

**Why this priority**: The headlines are how agents and models are compared; a wrong or unexplained number misleads every decision built on them.

**Independent Test**: Run an evaluation with two agents over a dataset with one anomalous entry, and check each run's headlines, contributions and exclusion reasons against values computed by hand.

**Acceptance Scenarios**:

1. **Given** a run with one transient-error entry and one confirmation-seeking entry, **When** its headlines are computed, **Then** the first counts in no headline and the second counts in all three.
2. **Given** an evaluation with one completed run, **When** the administrator opens it, **Then** Overall Performance reads N/A, "too few comparable runs"; **When** a second run completes, **Then** both get Overall Performance and the best run scores 100 on each shared metric.
3. **Given** the inherited selection and runs that measured time to first token, **When** the headlines are computed, **Then** `performance.latency.ttft` is listed as not a default contributor; **When** the administrator selects it, **Then** every run's headlines are recomputed without running any agent.
4. **Given** run A measured `performance.reliability.tool_error_rate` and run B did not, **When** Overall Performance is computed, **Then** the metric counts for neither, and A's record gives "missing from other runs" naming B.

---

### User Story 7 - Reuse earlier answers and rescore (Priority: P2)

When an entry's inputs have not changed, a run serves the stored answer instead of running the agent again, and computes every score afresh. Rescore scores a run's stored answers again on demand.

**Why this priority**: Agent runs are the main cost of evaluation, and most changes between runs touch only the scoring.

**Independent Test**: Run an unchanged evaluation twice with reuse on: the second run calls no agent, marks every entry reused and computes every score again.

**Acceptance Scenarios**:

1. **Given** reuse on and one completed run, **When** the evaluation runs again unchanged, **Then** every entry with a valid source is reused and the run list reads "{reused} of {total} predictions reused".
2. **Given** the same evaluation after the agent's persona was edited, **When** it runs again, **Then** no entry is reused.
3. **Given** an earlier result that asked for confirmation instead of answering, **When** a reuse-on run reaches the same entry, **Then** the agent runs it again.
4. **Given** a completed run, **When** the administrator rescores it, **Then** the judge and quality metrics are recomputed from the stored answers and the performance metrics are unchanged.

---

### User Story 8 - Compare runs (Priority: P2)

An administrator labels runs, marks a baseline agent, finds runs across evaluations and compares them side by side with the conditions that differ.

**Why this priority**: Comparison turns a list of scores into a decision.

**Independent Test**: Select two runs of one evaluation and open Compare: both runs' headlines, each entry's score per run, their tags and a parameter diff are shown.

**Acceptance Scenarios**:

1. **Given** an evaluation tagged `team=red`, an agent tagged `variant=a` and one of its runs tagged `variant=b`, **When** the run is read, **Then** its effective tags are `team=red` and `variant=b`.
2. **Given** agent A is the baseline, **When** the administrator makes agent B the baseline, **Then** A is no longer the baseline and every run of B is marked baseline.
3. **Given** two runs that differ only by model, **When** they are compared, **Then** the parameter diff lists `model` as changed and every other parameter as identical.

---

### User Story 9 - Move an evaluation between instances (Priority: P3)

An administrator exports an evaluation with its agents, datasets, runs and results as one package and imports it on another instance.

**Why this priority**: Useful for sharing and migration; evaluating on one instance does not need it.

**Independent Test**: Export an evaluation with two completed runs and import it on another instance: the evaluation, its agents and datasets, both runs and every result appear with the same scores and flags.

**Acceptance Scenarios**:

1. **Given** a package from another instance, **When** the administrator imports it, **Then** a new evaluation owned by the importer appears, linked to newly imported agents and datasets, with the runs and results as exported.
2. **Given** a package with a run that was running at export, **When** it is imported, **Then** that run reads failed.
3. **Given** a package of another format version or package type, **When** it is imported, **Then** it is refused with the reason and nothing is created.

---

### Edge Cases

- Two manual launches of one evaluation overlap: each executes only the runs it created (FR-026).
- Agentic AI unavailable when a run should start: the run stays pending, shown as held with the reason, up to the platform's hold limit (one week by default), then fails (FR-037).
- The agent, the evaluation or a replayed version is gone before the run starts: the run fails, naming what is missing (FR-037, FR-041).
- A stalled worker completes a run that a later attempt already failed: the completion stands (FR-039).
- A non-LLM agent in an LLM Only evaluation: it runs its own path and its outputs are never flagged as anomalies (FR-026, FR-036).
- Prompt guards or tool approval on an LLM Only evaluation: no effect, and the run records them off (FR-022, FR-029).
- An answer that is valid JSON but violates the schema: `json.schema_validity` is 0 while `json.field_accuracy` still scores the fields (FR-054).
- Nothing expected and nothing given (`{}` against `{}`, no labels, no STIX object or relationship): the entry scores 100 (FR-049).
- An empty PROSE expected answer: the entry is left out with the reason, never scored (FR-049).
- More than 10,000 pairs to compare in a list of objects, or 100,000 in one answer: `json.field_accuracy` scores 0 when an answer shaped like the expected one could be compared, else it is skipped with the sizes (FR-049, FR-050).
- Every field empty on one side only (`{"a": []}` against `{"a": "x"}`): `json.field_accuracy` is skipped, nothing compares (FR-049).
- An extractor whose path matches nothing scores 0, like an answer missing the field: it cannot tell an author's typo from an omitted field, so the author previews it on a stored answer before running (FR-047, FR-048).
- A STIX object of a type no library knows (a custom `x-` type), or a valid one a library cannot identify (a malware without `name`): it matches an object saying the same, whoever wrote it, else is a miss (FR-051).
- The same STIX content written another way (`Evil.com`, a hash in capitals, `…00Z` against `…00.000Z`, pattern spacing, `Uses`, other raw ids): it matches (FR-051).
- Two STIX objects sharing an id: the latest `modified` counts, the last one on a tie (FR-051).
- A crash in quality scoring: the entry keeps its answer and judge verdict, its quality left out with the error, on a rescore too (FR-049).
- Several serving models, one of them unpriced: cost is unavailable (FR-070).
- Best performance value of 0 or less: the tolerance band is taken from the span of values, so the best run still scores 100 (FR-082).
- Explicit empty selection: the headline reads N/A and lists no exclusion (FR-084).
- Runs started in one launch never reuse each other's answers: a source must already be completed (FR-096).
- A reused result's source run is deleted: the result stays marked reused, without a source link (FR-100).
- Imported results are never a reuse source: the package carries no fingerprint (FR-113).

## Requirements *(mandatory)*

### Functional Requirements

#### Datasets (US1)

- **FR-001**: An entry MUST have an expected answer and MAY have an input prompt, a description, its own judge instructions and attached files.
- **FR-002**: A dataset's family MUST be `CLASSIFICATION`, `STIX_BUNDLE`, `STRUCTURED_JSON` or `PROSE`, or unset and shown as "Needs family".
- **FR-003**: A CLASSIFICATION dataset MAY declare a non-empty label vocabulary, and every expected answer's labels MUST belong to it, checked when an entry is written or the vocabulary narrowed.
- **FR-004**: A STRUCTURED_JSON dataset MAY declare an answer schema (JSON Schema 2020-12, patterns screened for catastrophic backtracking), and every non-empty expected answer MUST conform to its entry's own schema when it declares one, else to the dataset's, when the schema or the entry is saved; a non-empty STIX_BUNDLE expected answer MUST be a JSON object.
- **FR-005**: A refusal under FR-003 or FR-004 MUST name the entry and the offending label or answer location.
- **FR-006**: Changing a dataset's family MUST clear a vocabulary or an answer schema the new family does not use.
- **FR-007**: An administrator MAY seed an answer schema from an agent's declared output schema, after a preview.
- **FR-008**: A read-only dataset MUST refuse every change except turning read-only off.
- **FR-009**: Every change to a dataset's entries or files, and every creation or import that brings entries, MUST create an immutable version freezing the entries, the vocabulary and the answer schema. The family is not frozen: a run takes it from the evaluation.
- **FR-010**: A run MUST pin the current version of every linked dataset when created (creating version 1 if none exists) and MUST read its entries, vocabulary and schema only from those versions.
- **FR-011**: Linking a dataset to an evaluation MUST be refused when both have a family and they differ, and a later family change MUST report every link that no longer matches without removing it.
- **FR-012**: A dataset MAY be exported and imported as a ZIP package of at most 500 MB, or imported from Hugging Face up to 500,000 rows.
- **FR-013**: In XTM One and development modes, the platform MUST provide a built-in OCR benchmark scored as `PROSE`: a dataset re-synced at every start, family included, and an evaluation created once, then left to administrators, the seed only filling a missing family. A sync that changes what a version snapshots MUST cut a new version once the documents are attached, so the next run scores the new documents.

#### Evaluation configuration (US2)

- **FR-014**: Every evaluation and dataset page and action MUST be restricted to administrators, who see and edit every evaluation.
- **FR-015**: Creating an evaluation or a dataset MUST require AI enabled and agentic AI licensed, other changes MUST require AI enabled, and reading and exporting MUST remain available with AI off.
- **FR-016**: An evaluation MUST have at least one agent and one model source: the agents' own model (on by default) or an extra model.
- **FR-017**: A disabled extra model MUST stay in the evaluation with its history and MUST NOT be run. An extra model no provider of the evaluation's owner would answer (a chat model it owns by catalog or vendor prefix) MUST NOT run on another model: every launch (FR-027) MUST refuse it (422 naming it), and a run whose model stopped being offered after it was created MUST fail with that reason before calling a model.
- **FR-018**: An evaluation MAY be saved without a family but MUST NOT run until it has one (FR-027).
- **FR-019**: An evaluation MUST store an execution mode (Full Loop by default, or LLM Only), a run mode (parallel by default, or sequential), a timeout (3600 s by default, at least 60 s) and a reuse default (on).
- **FR-020**: The LLM judge MUST be on by default and MAY be turned off for any family (judge settings: FR-057 to FR-060).
- **FR-021**: Each agent MAY have one extractor (FR-046) whose output kind fits the family, and a family change that would orphan an extractor MUST be refused.
- **FR-022**: "Apply prompt guards" and "Require tool approval" MUST be off by default, so the evaluated agent's Full Loop turns skip those gates, and the editor MUST warn which connected guard provider or "always ask" tools that skips.
- **FR-023**: Create, copy, export and import MUST carry every setting of an evaluation and of its agents (extractor, tags, baseline), read from one list. Copying an evaluation MUST create a private, enabled "Copy of …" with every setting, agents and dataset links, and no triggers or runs. An update MUST leave unchanged a setting it omits, MUST clear a nullable setting it sends as `null` (the judge model back to the platform default, the field overrides removed), except the family and the judge selection, which `null` leaves unchanged, and MUST merge `trigger_config` into the stored one: a key it leaves out is kept, a key it sends as `null` is removed, and an empty object changes nothing. Import MUST refuse a setting create refuses, with a 422 naming the field, before anything is imported (FR-114). The retired `quality_weight` is no setting: create, update and import MUST accept it and ignore it, and responses and exports MUST NOT carry it.
- **FR-024**: Deleting an evaluation MUST delete its links, runs, results and metrics.

#### Running (US3)

- **FR-025**: Runs MUST start from four triggers: manual (the Run action, the API or the built-in agent tool), scheduled (a cron expression checked every minute), callback (FR-032) and on agent change (saving a linked agent).
- **FR-026**: A launch MUST create one pending run per agent and model (the agent's own model when enabled, plus the enabled extra models), and exactly one model-independent run for a non-LLM agent; a manual launch MAY name the models instead.
- **FR-027**: Every launch (the Run action, the API, the agent tool, replay, bulk re-run, a schedule and a persona-improvement candidate) MUST pass the same checks and record the same snapshot (FR-029): it MUST be refused, with its reason and before creating any run, when the evaluation or a linked dataset has no family, callback mode has no resume secret, or there is no agent, model or entry. A refused scheduled launch MUST create no run, MUST log its reason once, and MUST start at the first check after the cause is fixed.
- **FR-028**: An evaluation's `enabled` flag, shown as "Automatic runs", MUST pause the scheduled and on-agent-change launches only: a launch a person starts MUST run whatever it says. A scheduled launch MUST skip an evaluation with a pending or running run.
- **FR-029**: A run MUST record at creation the agent's persona, prompt, configuration and skill versions and its pinned dataset versions, and at start its execution mode and whether prompt guards and tool approval applied, and MUST display these recorded values, never the current settings.
- **FR-030**: In Full Loop an entry MUST run the agent with its tools, as the evaluation's owner, on the run's model; in LLM Only an entry MUST be one model call with the persona and the entry, up to 4096 output tokens, retried once with a larger budget when cut. Both modes MUST deliver the answer of an agent declared as a JSON producer as bare JSON, as a chat turn does.
- **FR-031**: Every entry MUST run in autonomous mode: the agent is told that no human can answer and that it must deliver, and is never re-prompted.
- **FR-032**: With callback mode on (set through the API), an entry MUST pause after the agent's first turn until a resume signed with the evaluation's secret delivers its final output or an error, or until the callback timeout closes it as timed out. The paused entry MUST record what its first turn produced (the dataset snapshot entry, timeline, trace ids, tools, durations, call ledger and performance metrics), and its final output MUST then be scored as a fresh entry's: judge, quality and performance metrics, on the live entry when that snapshot was deleted before the callback came, and without the legacy token F1 of an answer it replaces. An entry paused before that record existed is judged on the live entry, nothing else known.
- **FR-033**: Runs MUST execute up to 5 at a time in parallel mode or one at a time in sequential mode, with each run's entries one after another, and one run's failure MUST NOT stop the others.
- **FR-034**: An entry MUST wait for the tasks its agent delegated, within the platform's wait budget and the launch's deadline.
- **FR-035**: An entry whose execution raises MUST be recorded with its error as the output and a score of 0, and the run MUST continue.
- **FR-036**: Each Full Loop or LLM Only output MUST be flagged as a transient error when it reports a transient provider failure (rate limit, overload, 5xx, timeout), else as confirmation-seeking when it ends by asking a question or offering options to choose from (counting: FR-075).
- **FR-037**: A run MUST end in exactly one terminal state: completed when every entry is processed, failed when it cannot finish (a callback run also when an entry failed or timed out), or cancelled on request, and a cancelled run MUST never be rewritten.
- **FR-038**: Cancel MUST apply to a pending or running run, keep its recorded results, and stop its work before its next entry.
- **FR-039**: A run MUST be executed by at most one worker, and a run that a dead worker left running MUST be failed with a message to re-run it, never executed again.
- **FR-040**: A launch MUST end by its deadline, the evaluation's timeout plus 120 s after its creation, whatever its attempts (at most 3), and a run it leaves unfinished MUST be failed at the latest by the stranded-run sweep 600 s later; a run waiting for a callback is not stranded.
- **FR-041**: Replay MUST create one new run with the original run's agent, model, versions and dataset versions, and MUST be refused when any of them no longer exists; "Re-run full" on a run's page is a replay without reuse.
- **FR-042**: Bulk "Re-run full" on 1 to 200 runs of one evaluation MUST create one new run per selected run with its agent, model and dataset versions, the agent's current versions and no reuse.
- **FR-043**: Deleting a run or all runs of an evaluation MUST be allowed in any status and with AI off, and MUST recompute the headlines (FR-089).
- **FR-044**: Evaluation entries, with their consults and transfers, MUST NOT be charged to the agentic quota.

#### Quality scoring (US4)

- **FR-045**: Quality metrics MUST score the extracted answer: the agent's final answer, or what its extractor returns; the expected answer is never transformed.
- **FR-046**: An extractor MUST read the final answer or the input or output of the last successful call to a named tool, apply 1 to 8 operations from a closed set, and produce a label set (CLASSIFICATION) or an object or array (STRUCTURED_JSON).
- **FR-047**: A failed extraction MUST give the entry one extraction failure with a reason from a closed set, the quality metrics the entry scores MUST score 0 with that reason when the answer caused it, and its family's metrics MUST be left out with it when the extractor's configuration did, and the run MUST count its extraction failures apart from its scores.
- **FR-048**: A successful extraction MUST be stored on the result, and an administrator MAY preview an extractor on a stored result or a pasted sample without saving it.
- **FR-049**: Every quality metric MUST report one status: scored (0–100), skipped (does not apply), unavailable (could not be measured, with a reason) or invalid (0, after an extraction failure), and a missing value MUST read N/A, never 0. Nothing expected and nothing given MUST score 100, except an empty PROSE expected answer, which is left out; an entry whose expected answer or configuration cannot be scored, or whose scoring fails, MUST be left out with the reason, on a rescore too, never keeping a previous value.
- **FR-050**: An ill-formed answer (unparseable JSON, schema violation, malformed STIX bundle, label outside the vocabulary) MUST be scored 0 with its diagnostic; when the answer wraps valid JSON in a code fence or text, the diagnostic MUST say so.
- **FR-051**: Each family MUST compute the metrics below, all higher-is-better, each rolled up over a run as its definition says (FR-077, FR-078):

  | Family | Metric key | Measures | Roll-up |
  |---|---|---|---|
  | `STRUCTURED_JSON` | `json.schema_validity` | the answer parses and conforms to the answer schema | pooled (pass/fail) |
  | | `json.field_accuracy` | weighted share of per-field credit | pooled |
  | `CLASSIFICATION` | `classification.vocabulary_validity` | every predicted label is in the vocabulary | pooled (pass/fail) |
  | | `classification.micro_prf` | label F1 over every label of every entry | pooled |
  | | `classification.macro_prf` | mean label F1 over the labels seen in the expected answers or the agent's | macro |
  | | `classification.samples_f1` | per-example F1 | averaged |
  | | `classification.subset_accuracy` | exact label-set match | pooled (pass/fail) |
  | `STIX_BUNDLE` | `stix.envelope_validity`, `stix.semantic_validity` | well-formed bundle; valid STIX 2.1 objects | pooled (pass/fail) |
  | | `stix.entity_prf` | F1 over canonical entity identifiers | pooled |
  | | `stix.entity_prf.by_type` | mean F1 over the entity types seen on either side | macro |
  | | `stix.relationship_prf` | F1 over (source, relationship type, target) triples, a sighting as (sighted, `sighting`, place) per place | pooled |
  | `PROSE` | `prose.exact_match` | normalised answer equals the expected one | pooled (pass/fail) |
  | | `prose.token_prf`, `prose.rouge_l` | token-overlap F1; longest-common-subsequence F1 | averaged |

  An F1 pools 2TP of 2TP + FP + FN; a pass/fail metric pools 1 or 0 of 1 per entry. An answer a metric cannot read (FR-050, an extraction the answer failed, a run the agent failed, FR-075) scores 0 and counts as the expected answer would: none of its pooled weight earned, every class it holds missed; one item when the expected answer is too large to weigh.

  A STIX object MUST be identified by what it says: timestamps, relationship types, domain names, hashes, names and indicator patterns compared normalised; the official identifier computed without the authoring metadata and after the objects it refers to; an object no library identifies by its content; objects sharing an id as versions, the latest counting.

- **FR-052**: The result view MUST show each P/R/F1 metric's precision, recall, F1 and counts (per type for `stix.entity_prf.by_type`), and the missing, extra, incorrect, correct and excluded fields of `json.field_accuracy`.
- **FR-053**: CLASSIFICATION MUST compare labels case-folded and without the marks around them (`Phishing.` is `phishing`, `C#` stays), MUST count empty expected and predicted label sets as correct and an out-of-vocabulary label as a false positive, using the linked datasets' vocabulary, else the labels found in the expected answers.
- **FR-054**: `json.field_accuracy` MUST score each leaf field by a mode derived from the answer schema (a nullable type by its non-null type, an array of plain values as a set), score an omitted field 0 whether `required` or not and an extra field 0, leave out an empty value on one side only, count a nested object and each entry of a list of objects as their leaves, and pair list entries for the most credit, so their order never moves the score.
- **FR-055**: An evaluation MAY override a field's mode (including excluded) and weight (at least 0, default 1) by JSON Pointer, and a contradictory or unknown override MUST be refused on save.
- **FR-056**: PROSE MUST normalise both sides one platform-wide way (NFC, case-folded, whitespace collapsed, the marks that present a word trimmed and those that identify it kept: `evil.com.` is `evil.com`, `C#` and `-5` stay). The required and forbidden phrase metrics MUST be neither offered nor stored until phrase lists are configurable.
- **FR-057**: When on, the LLM judge MUST receive the input, the agent's output, the expected answer, the tools available, the tool calls made and the instructions (the entry's, else the evaluation's, else the dataset's), the same for a fresh, reused, rescored, replayed or callback-resumed entry (each against the dataset snapshot the run was scored on, a callback-resumed entry with its first turn's tools and tool calls; a rescore keeps the verdict of an entry deleted since), and return five sub-scores from 0 to 100 with its reasoning; its prompt MUST describe each of these inputs.
- **FR-058**: The judge score MUST be the weighted sum of the sub-scores (tool selection, completeness, accuracy, instruction adherence, coherence; 0.20 each by default), rounded to 2 decimals.
- **FR-059**: The judge MUST run at temperature 0 with the evaluation's judge model (else the platform default), its prompt (else the managed judge skill) and an output budget of 8192 tokens by default (256 to 131,072), and tool selection MUST be 100 without asking the judge when no tool is available. A reused, rescored or replayed entry MUST be judged against the tools its agent had when the answer was produced, recorded with the result.
- **FR-060**: A malformed verdict MUST be repaired when possible, and one that stays unusable, truncated or incomplete MUST be a judge failure, with no retry and no substitute score.
- **FR-061**: A run MUST record the metric suite version it was scored with, "unknown" when unreadable.
- **FR-062**: When the metric package is missing or incompatible, scoring MUST fail closed, naming the package, and never use a partial metric set.

#### Performance measurement (US5)

- **FR-063**: The system MUST measure nine performance metrics per entry:

  | Metric key | Unit | Direction | Roll-up |
  |---|---|---|---|
  | `performance.latency.wall_clock` | ms | lower is better | averaged |
  | `performance.latency.ttft` | ms | lower is better | averaged |
  | `performance.counts.model_calls` | count | lower is better | averaged |
  | `performance.counts.tool_calls` | count | lower is better | averaged |
  | `performance.counts.retries` | count | lower is better | averaged |
  | `performance.tokens.total` | tokens | lower is better | averaged |
  | `performance.cost.llm` | USD | lower is better | averaged |
  | `performance.reliability.completion_rate` | ratio | higher is better | pooled |
  | `performance.reliability.tool_error_rate` | ratio | lower is better | pooled |

- **FR-064**: A value that was not captured MUST be unavailable with a reason, an observed zero MUST be scored 0, and malformed telemetry MUST make all nine invalid without failing the entry.
- **FR-065**: Wall-clock latency MUST be timed around the agent call and end at the answer.
- **FR-066**: Time to first token MUST end at the first text of the model call that produced the answer (Full Loop) or at the first streamed text (LLM Only), and MUST be unavailable when that answer was retracted, transferred or not streamed.
- **FR-067**: Total tokens MUST be uncached input plus output plus cache-read plus cache-write tokens; reasoning tokens are reported, not added.
- **FR-068**: In Full Loop, tokens, cost, calls and retries MUST cover the agent, the agents it consulted or transferred to, and the tasks they delegated, transitively, and never the judge.
- **FR-069**: When that chain is incomplete (wait bound reached, run cancelled, record missing), tokens, cost, calls, retries and both rates MUST be unavailable ("chain incomplete"), never partial.
- **FR-070**: Cost MUST be each serving model's tokens at that model's prices, and MUST be unavailable, never 0, when a price is missing or currencies differ.
- **FR-071**: Completion rate MUST be successful calls over all model and tool calls, and tool error rate errored tool calls over tool calls (skipped with no tool call), each keeping its counts.
- **FR-072**: Each result MUST record Judge Time when the judge ran (with its timeline step, replaced when a rescore, a replay or a resumed callback judges it again), and Run Duration (agent plus judge when fresh or resumed from a callback, judge only when reused or rescored), which enter no headline.

#### Headlines (US6)

- **FR-073**: Each run MUST carry three separate 0–100 headlines, Judge Quality, Overall Quality and Overall Performance, never combined into one score.
- **FR-074**: A headline or run value that was not measured MUST read N/A, never 0, and MUST be left out of rankings and charts.
- **FR-075**: A headline MUST count only entries that are not transient-error anomalies and whose judge did not fail; confirmation-seeking entries count. An entry whose agent run raised MUST be treated the same by the three headlines, by the cause the failure classifier reads: a provider failure (rate limit, overload, timeout, connection, every provider failed) is a transient-error anomaly; a request or budget the agent's configuration shapes (context overflow, request too large, request rejected or declined by the model, iteration budget exhausted) scores 0 in Judge Quality and in every quality metric and counts one failed call in the completion rate; anything else (configuration, licence, quota, a platform error, unknown) is a platform-error anomaly, left out of every headline and counted by Overall Quality's coverage as left out with its reason, and every surface that lists or compares runs MUST show how many entries of a run it left out (`platform_error_count`). The run's sub-score averages MUST count the same entries as Judge Quality, and a run scored with the judge off has none.
- **FR-076**: Judge Quality MUST be the mean of the counted judge scores minus the spread penalty, and MUST read N/A when the run was scored with the judge off.
- **FR-077**: An averaged metric's run value MUST be the mean of its counted entry values moved toward worse by the spread penalty, k × the standard deviation (k = 0.5 by default, set per evaluation), after flipping lower-is-better quality values to 100 − v. A pass/fail metric is pooled (FR-078), never penalised: on a 0-or-100 value the spread is a function of the share of passes.
- **FR-078**: A pooled metric's run value MUST be its counted entries' summed numerators over their summed denominators, with no penalty, and none when an entry lacks its counts or the denominator is 0, except a quality score whose entries all hold 0 of 0 (nothing expected, nothing given), which reads 100. A macro metric's run value MUST be the unweighted mean F1 of its classes, each class's counts summed over the counted entries, over the classes seen in the expected answers or the answers, with no penalty; none when an entry lacks its counts; the mean of its entry values when no entry saw a class. The metrics panel keeps every metric's per-entry statistics. A run scored before class counts were kept has its counts rebuilt from its stored results on upgrade.
- **FR-079**: A metric whose direction or roll-up is unknown or declared inconsistently MUST compose no headline.
- **FR-080**: Overall Quality MUST be the equal-weight mean of the run's contributing quality metric values, from that run alone.
- **FR-081**: Overall Performance MUST be computed across the evaluation's completed runs from the performance metrics that every measuring run has; a run that measured none abstains, and a measured 0 counts.
- **FR-082**: Each shared performance metric MUST be normalised across runs: 100 within the tolerance band of the best value (5 % by default, set per evaluation), 0 for the worst, linear in between, 100 for all when values are equal, and N/A when fewer than two runs carry it.
- **FR-083**: Overall Performance MUST be the equal-weight mean of the run's normalised metrics, MUST read N/A with fewer than two comparable runs, and MUST be labelled comparative wherever it is shown.
- **FR-084**: Each headline's metric selection MUST be inherit (the default contributors), none (N/A) or an explicit list; the default quality contributors are STIX `entity_prf` and `relationship_prf`, JSON `field_accuracy`, CLASSIFICATION `micro_prf`, `macro_prf` and `samples_f1`, and PROSE `token_prf`; every performance metric is one except `performance.latency.ttft`.
- **FR-085**: Each run MUST record per headline its contributing metrics with their contribution and counted-entry count, and each excluded metric with its reason and, for Overall Performance, the runs concerned. Overall Quality MUST also record how many of the entries it considers it covers and, per reason, why the others are left out, shown as "computed over N of M entries" when it covers fewer.
- **FR-086**: An exclusion reason MUST be `not_applicable` (a re-run cannot help), `failed` (a re-run may help), `not_a_default_contributor`, `missing_from_other_runs` (naming the runs lacking it), or for the whole run `no_performance_data` or `too_few_comparable_runs`.
- **FR-087**: The system MUST disclose every selected metric that did not contribute and, under the inherited selection, every non-default metric that produced a value and every metric the shared-metric rule dropped.
- **FR-088**: A headline that reads N/A MUST show its cause.
- **FR-089**: Headlines MUST be recomputed from stored values, without running any agent, when a run completes, is rescored or deleted, a judge result is replayed, or the tolerance, the spread penalty or a selection changes, refreshing Overall Performance for every completed run of the evaluation.
- **FR-090**: When an evaluation's orchestration fails, the system MUST compute the headlines of every completed run left without them.
- **FR-091**: The run metrics panel MUST describe every entry, anomalous and judge-failed ones included, and MUST mark the metrics whose run value pools counts.
- **FR-092**: A run scored before these headlines existed MUST show its stored scores labelled "(legacy)" and never ranked.

#### Reuse and rescore (US7)

- **FR-093**: A run's reuse MUST follow the manual trigger's override (on, off or inherit), else an evaluation run parameter named `reuse`, else the evaluation's reuse default, and MUST be off for "Re-run full" and in callback mode.
- **FR-094**: Every result MUST record a fingerprint of the inputs that shape the answer: agent, model, execution mode, entry input and files, persona, prompt and configuration versions, the output format and schema of an agent declared as a JSON producer, and for Full Loop each bound knowledge base's revision and embedding model, a raised iteration cap, and the prompt-guard and tool-approval switches when on.
- **FR-095**: The fingerprint MUST NOT include the scoring configuration, so a scoring-only change reuses every answer.
- **FR-096**: A result MAY be a reuse source only when it is a fresh, non-anomalous, non-error answer of another completed run of the same evaluation with the same fingerprint, and the most recent valid one among the 5 latest wins.
- **FR-097**: A candidate whose stored answer reads as confirmation-seeking or as an error MUST NOT be reused.
- **FR-098**: A reused entry MUST copy the source's answer and telemetry and recompute its anomaly flag, judge, quality and performance metrics and headlines, never copying scores.
- **FR-099**: A reused entry's performance metrics MUST describe the source execution and count like a fresh entry's.
- **FR-100**: The run list MUST show how many entries were reused, and the run page MUST mark each reused result with its source.
- **FR-101**: Rescore of one run or up to 200 runs MUST score the stored answers again (anomaly flags, judge, quality metrics with the current extractor, headlines) without running the agent, leaving answers, performance metrics, a legacy token F1 and fingerprints unchanged. Its quality configuration is built from every entry the run scores, as the fresh pass builds it, so a cancelled, failed or timed-out run keeps its CLASSIFICATION vocabulary.
- **FR-102**: Rescore MUST refuse a pending or running run or one with no stored answers, and a bulk rescore MUST report each refused run with its reason.
- **FR-103**: Judge-only replay MUST judge one result again with a chosen output budget (256 to 131,072 tokens, 2048 by default) and recompute the headlines.

#### Comparison (US8)

- **FR-104**: Tags MUST be settable on an evaluation, on an agent within it and on a run, and a run's effective tags MUST be their merge at read time, run over agent over evaluation.
- **FR-105**: Tag values MUST be stored as strings, and the reserved key `baseline` MUST be dropped.
- **FR-106**: An evaluation MUST have at most one baseline agent, and every run of it MUST read as baseline.
- **FR-107**: Every run MUST record its run parameters with their source: auto-captured conditions, the evaluation's custom parameters and the manual trigger's parameters, a manual value winning over an evaluation value over an auto-captured one.
- **FR-108**: The parameter diff of two runs, from any evaluations, MUST list each key as changed or identical, with both values.
- **FR-109**: Run search MUST cover every evaluation and filter by parameter, metric aggregate, headline, effective tag, baseline, pinned dataset version and date, newest first, 100 results by default and 500 at most, and an unknown filter key MUST match no run.
- **FR-110**: The compare page MUST show, for two or more runs, their configuration, headlines, tags and per-entry scores, and the parameter diff when exactly two are compared.
- **FR-111**: The agent tools that read, start or compare evaluations MUST serve administrators only, and comparing MUST take two completed runs of the same evaluation and agent and give a delta and a verdict (±2 points) for Overall Quality, Overall Performance, the judge's score and the token F1.

#### Export and import (US9)

- **FR-112**: Export MUST produce one versioned ZIP package with the evaluation's definition, its agents and datasets in their own export formats, and every run with its results, each result referencing its entry by dataset and position. A position MUST be unique in its dataset: an entry, or a dataset linked to an evaluation, added at the end takes the next one.
- **FR-113**: The package MUST carry the evaluation's whole configuration and each result's answer, scores, reasoning, durations, execution details and flags, but no fingerprint and no reuse source.
- **FR-114**: Import MUST refuse, with the reason and leaving nothing behind, a file that is not a valid package of format version 1, holds a value of the wrong type or range (naming the field), or lacks an embedded agent or dataset it lists, and MUST validate the settings as creating an evaluation does. It MUST be one transaction, embedded agents and datasets included: a package that fails halfway leaves no row and no stored file behind.
- **FR-115**: Import MUST create a private evaluation (its name made unique) with automatic runs paused, so the source instance's schedule never fires unattended, import the embedded agents and datasets as new objects, keep each run's terminal status (any other becomes failed) and each result's flags, and derive each result's metrics from its execution details.
- **FR-116**: Import MUST compute the headlines of every imported run as its completion does, from its results, metrics and scorer selection, so it shows the headlines it had on its source.
- **FR-117**: Replaying an imported run MUST be refused, since its versions are those of the source instance.

### Key Entities

- **Dataset**: a named, ordered set of entries with a family, an optional label vocabulary or answer schema, a read-only flag and a current version.
- **Entry**: an input prompt, its expected answer, optional judge instructions, attached files and an optional answer schema of its own.
- **Dataset version**: an immutable snapshot of the entries, vocabulary and answer schema, numbered from 1.
- **Evaluation**: the configuration: agents, models, linked datasets, family, judge and scoring settings, run settings, headline selections, triggers, tags and an enabled flag.
- **Evaluation agent**: an agent linked to an evaluation, with its extractor, its tags and whether it is the baseline.
- **Run**: one agent and one model over the pinned dataset versions, with its status, trigger, version snapshot, recorded settings, progress, anomaly count, headlines and their provenance.
- **Launch**: one start of runs (a trigger, a replay, a bulk re-run, a rescore or a resume), owning the runs it created, with a deadline and up to 3 attempts.
- **Result**: one entry's outcome in a run: the answer, the extracted answer, the judge verdict, the anomaly flag, the fingerprint, the reuse source, durations and execution details.
- **Metric value**: one metric's status and value for one result, with its counts when pooled.
- **Run parameter**, **Tag**: a run's recorded conditions with their source; key/value labels at evaluation, agent and run level.
- **Export package**: a versioned ZIP holding an evaluation, its agents, datasets, runs and results.

### Known gaps

Each gap names the requirement it departs from.

- The on-agent-change trigger creates no run (FR-025).
- Run mode, spread penalty and sub-score weights are not validated on save (FR-019, FR-058, FR-077); create links datasets without the family check (FR-011).
- Anomalies are detected from the answer's wording, so a complete answer that ends with a question is flagged confirmation-seeking and a provider error worded otherwise counts as a 0 (FR-036).
- A rescore keeps the performance and timing rows the new pass no longer produces and the original suite version (FR-061, FR-101), and changes the run's status; cancelling a rescore overwrites the run's timing; a run whose every entry raised ends completed (FR-037); the two "Re-run full" use different versions (FR-041, FR-042); deleting does not stop work in flight.
- Rates read "not captured" for an entry with no call (FR-064); some N/A causes are misnamed (FR-088); a performance metric has two names; evaluation text is partly untranslated; trigger-time tags keep `baseline` (FR-105); changing only a dataset's vocabulary or schema creates no version (FR-009).
- The built-in OCR benchmark is seeded only in the mode where the Evaluations page is hidden, plus development (FR-013).
- The OCR benchmark is scored as `PROSE`, whose token F1 counts a table's layout and misses cells swapped between rows; a Markdown family scoring cells by row is v2 (FR-013).
- `json.schema_validity` does not check `format`; `array_semantics` and `numeric_tolerance` cannot be set from the platform; `$ref` schemas are not followed; CLASSIFICATION micro and macro F1 are per-entry averages (FR-051).
- The fingerprint omits skill versions, tool bindings and handovers (FR-094).
- Run search filters only the newest runs and can miss older matches (FR-109).
- Nothing tells the reader that a reused run's Run Duration covers the judge only (FR-072, FR-099).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: No surface shows 0 for a headline, metric or cost that was not measured.
- **SC-002**: A headline setting change updates every run's headlines with 0 agent calls.
- **SC-003**: A second run of an unchanged evaluation with reuse on makes 0 agent calls.
- **SC-004**: Every run not held for unavailable AI reaches a terminal state within its evaluation's timeout plus 12 minutes of its launch, and none is executed twice.
- **SC-005**: Every metric left out of a headline is listed with its reason.
- **SC-006**: An evaluation exported and imported on another instance shows the same score for every result.

## Clarifications

### Session 2026-10-08

- Q: What does this specification describe? → A: Current behaviour; a defect is a known gap with its issue, never a requirement, so the specification stays true of the code.
- Q: Does the run metrics panel use the headline's entry filter? → A: No; it describes every entry (FR-091), so its mean can differ from a headline input.
- Q: Can an imported run be replayed? → A: No (FR-117); its versions exist only on the source instance.
- Q: Do tasks delegated inside an evaluation cost agentic quota? → A: Today they do, unlike entries (FR-044); whether they should is open.

## Assumptions

- Administrators are the only users of evaluation.
- The metric package ships with the platform and declares every metric's key, direction, unit and roll-up.
- Model prices come from the platform's price rows.
