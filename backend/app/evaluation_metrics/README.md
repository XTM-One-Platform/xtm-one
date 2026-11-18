# evaluation_metrics

Deterministic, unit-tested metrics for agentic LLM evaluation, in two suites: **quality**
(`quality/`, how right an answer is) and **performance** (`performance/`, what an execution
cost in time, tokens, money and failures). Pure functions with a uniform interface: no LLM
calls, no I/O, fail-closed, no platform imports.

The package scores one entry at a time; aggregation, headlines, execution and the LLM judge
are the platform's (`dev-docs/reference/evaluations.md`).

## Where it lives

    backend/app/evaluation_metrics/            the package
    backend/tests/test_metric_suite_*.py       its tests
    backend/tests/fixtures/metric_suite/       its test fixtures

Platform code imports `app.evaluation_metrics` only from
`app/services/evaluation/metric_package/` ([Boundary rules](#boundary-rules)). Third-party
dependencies (`pydantic`, `jsonschema`, `scipy`, `stix2`, `pycti`) are in
`backend/pyproject.toml`.

`__version__` (`app/evaluation_metrics/__init__.py`) is recorded on every run
(`EvaluationRun.metric_suite_version`). Bump it when a change can move a score or alter a
serialized result; its docstring is the changelog.

## The quality result contract

Every quality metric returns a frozen `ScoreResult`:

```python
ScoreResult(
    status: MetricStatus,          # scored | invalid | skipped | unavailable
    value: float | None,           # 0-100 when scored, 0 when invalid, else None
    detail: Mapping[str, JSONValue],
    error: MetricError | None,     # required when invalid
    metadata: MetricMetadata,      # key, label, kind, primary_measure, direction, rollup
                                   # (and shown_on_entry, entry_label, never serialized)
)
```

| Status | `value` | Meaning |
|--------|---------|---------|
| `scored` | 0-100 | The metric produced a measurement |
| `invalid` | `0` | The input is not of the type the metric reads (a prose prediction that is not text) |
| `skipped` | `None` | The metric does not apply to these inputs; `detail["reason"]` says why |
| `unavailable` | `None` | A required backend is missing: **not** a model failure |

A malformed answer the agent produced (unparseable JSON, a malformed STIX bundle, an
out-of-vocabulary label) is `scored`/0 with its reason in `detail`, never `invalid`: the
failure is the agent's, and only `scored` results reach a run's value.

`shown_on_entry` and `entry_label` say how a metric reads on a single entry. Most metrics mean
the same on one entry as over a run and keep the defaults (`True`, `None`); a metric that only
means something across entries is either not shown on one entry (`shown_on_entry=False`) or
shown under a narrower name (`entry_label`). The run's value is unaffected. Both say how a page
shows the metric, not what it returns, so `to_dict` and `to_json` leave them out.

Invalid caller configuration (missing schema, vocabulary or canonicalizer, unknown policy
option, mismatched collection lengths) raises `ConfigurationError`, never a score or a midpoint such as 50. `to_json()` is
byte-stable (sorted keys, compact separators, `NaN`/`Infinity` rejected).

### How a run combines a metric (`rollup`)

Every metric declares a required `rollup`; the platform applies it
(`dev-docs/reference/evaluations.md`).

| `rollup` | Value | A scored result carries | The run reads |
|---|---|---|---|
| `mean` | grades the whole entry (samples F1, token F1, ROUGE-L, a time) | nothing more | the entries' mean, less the platform's spread penalty (`dev-docs/reference/evaluations.md` §6) |
| `pooled` | a share of items counted inside the entry; a pass/fail metric counts one item | `detail["numerator"]`, `detail["denominator"]`, non-negative reals | sum of numerators over sum of denominators |
| `macro` | the mean F1 of the classes the entry saw | `detail["classes"]`: `{class: {tp, fp, fn}}` | each class's counts summed over the run, then the classes' F1 averaged |

An F1 pools `2TP` of `2TP + FP + FN`, `json.field_accuracy` its fields' credit over their
weight. `check_rollup_counts` refuses a scored result without what its roll-up needs.

**An answer a metric cannot read** (unparseable, malformed, too large to pair, not extracted)
scores 0 and weighs what the expected answer would: `unreadable_answer(expected, detail)`
takes the metric's result for the reference given as the answer and keeps its weight and its
classes, none of its credit. With `limited=True` (only a compute limit kept the reference from
scoring) the answer weighs one item.

## Generic primitives

`set_diagnostics`, `multiset_diagnostics` (duplicates matter), `precision_recall_f1` (0..1
scale) and `macro_average` (per-label P/R/F1 over the mapping's labels). Zero denominators
yield `0.0`, never `NaN`.

**Nothing expected and nothing given is correct.** An entry whose reference and answer are
both empty (no labels, no STIX objects, `{}` against `{}`) scores `EMPTY_ANSWER_SCORE` (100)
in every family; a pooled score adds 0 of 0 for it. The rule is the family's, not
`precision_recall_f1`'s: in `macro_average`, 0/0/0 means a label on neither side. PROSE is
the exception: an empty reference is a draft, never scored.

## Structured JSON (`STRUCTURED_JSON`)

`json.schema_validity` checks against a Draft 2020-12 schema; unparseable text scores 0.
`json.field_accuracy(reference, prediction, policy, schema)` scores each field under a
`ComparisonPolicy` that is **strict by default**: `exact` arrays positional, numbers exact, no
coercion, nothing ignored. Every relaxation is opt-in per JSON Pointer: `array_semantics`,
`numeric_tolerance`, `ignored_paths`, `field_modes` (override the derived mode),
`field_weights`. `array_semantics` applies to `exact` arrays only; override a field to `exact` when order counts.

Each field is scored under a mode derived from the schema (with no schema, every field is
`exact`), and the result is the weighted mean of the per-field scores:

| Declared shape | Derived mode | Scored as |
|---|---|---|
| `enum` or `const`, scalar | `enum` | exact membership |
| array whose `items` are objects, or an `anyOf` / `oneOf` of objects | `object_list` | entries paired, each counted as its leaves |
| any other array | `enum_set` | set F1, order-insensitive |
| `string` with a URI-ish `format` | `resource_address` | well-formed **and** matches under normalization |
| `string`, no `enum`/`format`/`pattern` | `free_text` | token F1, partial credit |
| anything else | `exact` | equality |

A nullable type derives the mode of its non-null type. A declaration is read through its
local `$ref` (`#/$defs/...`, any `#` JSON Pointer into the schema), the keywords beside the
reference kept over the target's; a reference to another document, or one that leads back to
itself, names no shape (`exact`). A schema with references scores as the same schema inlined.
In a list of objects of several shapes, each entry takes the branch it matches by the values
the branches fix differently (`const`, `enum`; an enum every branch shares tells nothing);
when none or several match, the properties every branch declares alike, annotations
(`description`, `title`, ...) aside. A pair is scored under its reference entry's branch, an
unpaired entry under its own; an object field of several shapes takes its value's branch.
`allOf` is not merged, and a union nested in a union is not a list of objects. **Leaves count, each once:**

* A nested object counts as its leaves, whether the answer holds it or not.
* A list of objects is paired by optimal assignment for the most credit (ties: fewest fields
  counted). A missing entry counts its reference leaves as missed, an extra one its leaves as
  extra. Entry order never moves the score.
* A field the reference holds and the answer omits is a miss, `required` or not. An empty
  value (`null`, `[]`, `{}`) on one side only is left out.
* `excluded` on a container drops its leaves; a container's weight multiplies theirs. A weight
  on an excluded or ignored path raises `ConfigurationError`.
* Pairing is bounded: 10,000 pairs per list (`MAX_ENTRY_PRODUCT`), 100,000 per answer
  (`MAX_PAIRS_SCORED`). Beyond either, the answer scores 0 (`prediction_too_many_entries`)
  when the reference paired with itself fits; otherwise `skipped`
  (`pairing_input_too_large`).
* The walk is bounded in depth: 64 levels (`MAX_FIELD_DEPTH`, JSON Pointer tokens). Deeper,
  the metric is `skipped` (`nesting_too_deep`, with the depth reached); the entry keeps its
  other metrics.
* `skipped` when every field is excluded, ignored or weighted 0 (`no_scorable_fields`), or
  empty on one side only (`empty_on_one_side`).
* An array of plain values is a set, compared in JSON form (`"1"` is not `1`).
* An explicit `free_text` requires a `prose_policy`; a derived one falls back to `exact`
  without it.

`detail["per_field"]` holds one entry per field; `correct`, `incorrect`, `missing`, `extra`
and `excluded` partition its pointers.

## Classification (`CLASSIFICATION`)

`label_vocabulary_validity(labels, vocabulary)` is the gate;
`classification_dataset(references, predictions, vocabulary)` returns the dataset metrics.

Macro P/R/F1 averages over the labels seen in a reference or a prediction; an unused declared
label tests nothing. An empty collection skips the dataset metrics (`no_examples`). An
out-of-vocabulary predicted label makes the gate `scored`/0, listing offenders in
`detail["offending_labels"]`; the dependent metrics still count it as a false positive.

`micro_prf`, `macro_prf` and `samples_f1` (labelled "(Micro)", "(Macro)" and "(Samples)") are three
ways of combining entries. On a single entry there is nothing to combine: `micro_prf` is the
entry's own label F1 (`entry_label="Label F1"`), `samples_f1` repeats it, and `macro_prf` becomes
the share of labels found among those expected or predicted, so both are `shown_on_entry=False`.

## STIX 2.1 (`STIX_BUNDLE`)

`stix_envelope_validity` is a **shallow** structural check and must never be presented as
full STIX 2.1 validity. `stix_semantic_validity` is `unavailable` without a validator backend.
`stix_entity_scores` and `stix_relationship_scores` require a canonicalizer (omitting it
raises `ConfigurationError`), so raw non-canonical IDs are never compared. Entity scoring
covers SDOs and SCOs; `relationship`, `sighting`, `marking-definition`, `language-content` and
`extension-definition` are excluded. Relationships and sightings are scored as canonical
triples, one per place sighted. `StixObjectTypeSelection` applies its allowlist, then its
excludelist.

The backends, `OpenCtiCanonicalizer` and `StandardsStixValidator` (`stix2.parse`, custom
objects allowed, failures as structured findings), run offline. `pycti` needs the
`libmagic` system library; without it, importing the canonicalizer raises `ImportError`.

`StandardsStixValidator` reads the bundle as written before `stix2.parse`, which would fill
in a missing `id`, `created` or `modified`. Its findings: `missing_required_property`
(an object without `id`, or an SDO, SRO, language content or extension definition without
`spec_version`, `created` or `modified`; a marking definition needs no `modified`),
`modified_before_created`, and `stix_parse_error` for anything else `stix2.parse` refuses.
Any finding makes the bundle invalid: `stix.semantic_validity` scores 0.

The canonicalizer identifies an object from what it says:

* **Normalized first:** timestamps, relationship types, domain names, IP addresses, hash
  names and digests, domain-object names and STIX patterns each take one form.
* **Official identifiers:** `pycti` for its types, `stix2` deterministic UUIDv5 for
  cyber-observables, asked first without authoring metadata (`created`, `created_by_ref`),
  then with it where the type needs it; never a random id.
* **References first:** objects are identified referenced first (strongly connected
  components), whatever the bundle order; a cycle's members see each other's type.
* **Never a crash:** an object neither implementation identifies gets a UUIDv5 over its
  non-common properties; `canonicalize` never raises on a list of objects. No raw-ID
  fallback, no defang normalization.
* **Versions:** objects sharing an id are versions of one; the latest `modified` wins, the
  last one on a tie.
* **As OpenCTI imports:** an attack-pattern without `x_mitre_id` takes the ATT&CK id of its
  last `mitre-*` external reference, a location without `x_opencti_location_type` its type
  from `city`, `country` or `region` (else Position), as pycti's `import_from_stix2` does.

The aligned scores pair objects within their ontology class (compatible classes for the soft
score), so their compute bound (`MAX_ENTRY_PRODUCT`) applies to each class's block, not to the
whole entry. Past it, as for `structured_json`, the answer is credited nothing
(`prediction_too_many_objects`: every reference item missed, every predicted one extra) when
the reference aligns with itself within the bound, else the entry is `skipped`
(`too_many_objects`); both report `largest_block_product`. Their relationship reading also
finds a reference relationship through a reference object the pairing left unpaired when the
predicted endpoint is of its class and carries one of its names: a reference that keeps one
entity as several objects is matched once per object. With the object types the run's dataset
references annotate (`FamilyConfig.reference_types`, derived by the platform from every
reference), `stix.entity_prf.aligned.closed` and `stix.relationship_prf.aligned.closed` score
the answer reduced to those types and the relationships between them, read as the pairing
reads them (`threat-actor` annotated means `intrusion-set` is, and a kept indicator keeps the
observable it is `based-on`): reported beside the aligned scores, never instead of them. A dataset none of whose references holds a relationship
(an indicator list) annotates none: its closed relationship score is `skipped`
(`not_annotated_by_dataset`), not 0 for every relationship an answer gives. Each aligned score's
`detail.by_type` gives precision, recall and F1 with their counts per object class (`malware`,
`identity:class` for a sector...) and per relationship type (`uses`, `targets`...): the entry's
one F1 hides a type an answer misses, and the rows add up to its counts.

## Prose (`PROSE`)

Deterministic, **lexical** scoring under a `ProseNormalizationPolicy`: exact match (100 or 0),
token F1, ROUGE-L (LCS F1), required-phrase coverage and forbidden-phrase absence.

> **Out of scope.** No semantic accuracy, factual correctness, entailment or hallucination
> detection, and no model- or embedding-based measure; never present the scores as such.

`prose.rouge_l` is the only order-sensitive prose metric (`"a b c d"` against `"d c b a"`:
100 on `prose.token_prf`, 25 here). **Computation bound:** exact LCS is `O(r x p)`
and prediction length is attacker-controlled, so above a token product of 5,000,000
(a product of exactly 5,000,000 is computed) the metric is `skipped` (`rouge_l_input_too_large`, with `reference_tokens`,
`prediction_tokens`, `token_product`, `max_token_product`) without running the LCS. The bound
is not caller-configurable; changing it is a versioned change. It bounds the LCS only:
bounding raw input size is the host's job.

### Normalization policy

Applied in the fixed order **Unicode form → case → punctuation → whitespace**. Defaults are
strict: only whitespace is collapsed.

| Option | Values | Default |
| --- | --- | --- |
| `unicode_form` | `NFC`, `NFD`, `NFKC`, `NFKD` | `NFC` |
| `case` | `preserve`, `casefold` | `preserve` |
| `punctuation` | `preserve`, `strip`, `edges` | `preserve` |
| `whitespace` | `preserve`, `collapse` | `collapse` |

- `NFKC`/`NFKD` fold compatibility forms (`ﬁle` → `file`).
- `strip` replaces every Unicode `P*` character with a space, `Pc` included: `snake_case`
  becomes two tokens.
- `edges` trims only what presents a token: sentence punctuation at its end, brackets and
  quotes at either end, `*` or backtick emphasis. `evil.com.`, `(T1059)` and `**APT29**`
  become `evil.com`, `T1059`, `APT29`; `C#`, `-5`, `.NET`, `::1` and `__init__` stay whole. A
  token of punctuation alone disappears.

Tokenization splits on Unicode whitespace. Markdown and HTML are raw prose: no parsing.

### Phrase matching

A phrase is present only as a **contiguous run** of the prediction's normalized tokens (`cat`
does not match `concatenate`). Phrases are deduplicated after normalization, matching
consumes no tokens, and the diagnostics list every phrase. `ProseConfig` rejects a phrase in
both lists; calling a phrase metric directly bypasses that check.

| Configuration | Result |
| --- | --- |
| `phrases=None` | `skipped`, `required_phrases_not_configured` |
| `phrases=()` | `skipped`, `required_phrases_empty` |
| `reference=None` | `skipped`, `reference_not_configured` |
| `reference=""` (or whitespace) | `skipped`, `reference_empty` |

A prediction that is not text is `invalid`/0 (`prose_input_invalid`); a reference that is not
text raises `ConfigurationError`. An unconfigured metric is `skipped` even when the prediction
is unusable.

## Family routing

`run_for_family(reference, prediction, family, config)` returns a deterministic mapping from
metric key to `ScoreResult`, identical to calling each metric directly. The families are
`STIX_BUNDLE`, `STRUCTURED_JSON`, `CLASSIFICATION` and `PROSE`. A missing required
configuration (`PROSE` without a `ProseConfig`), an unknown value or a `Family` member with
no runner raises `ConfigurationError`: no family is scored as another.

## Performance suite

`evaluation_metrics.performance` consumes **already-captured, caller-supplied telemetry** and
measures nothing itself (no clock, provider, network or filesystem). A metric returns a
frozen `PerformanceResult`, never interchangeable with `ScoreResult`:

```python
PerformanceResult(
    status: MetricStatus,          # scored | invalid | skipped | unavailable
    value: float | None,           # unbounded and finite when scored, else None
    detail: Mapping[str, JSONValue],
    error: MetricError | None,     # present iff status == invalid
    metadata: PerformanceMetadata, # key, label, kind, primary_measure, unit, direction, rollup
)
```

`invalid` (malformed telemetry) carries `value=None`, never `0`, which would read as a fast or
cheap execution. An `unavailable` result explains itself in `detail["reason"]`. `unit`
(`milliseconds | count | tokens | currency | ratio`) qualifies every value; keys are namespaced
`performance.`.

**Telemetry.** `ExecutionTelemetry` takes token buckets, call records and durations as
captured. `None` means *not captured* (`unavailable`); an observed empty value (`calls=()`,
a count of `0`) is a measurement (`scored`). `validate_telemetry(raw)` never raises on
malformed structure: it flows into `invalid` results. Only caller configuration defects (a
`PriceTable` missing a bucket) raise `ConfigurationError`.

**Cost** comes only from a caller-supplied `PriceTable`: the package holds no provider prices
and infers none. With several models, `estimated_llm_cost(telemetry,
prices_by_model={...})` prices each model's usage (`agent_token_usage_by_model`) at its own
table; a model without one, or one `prices` table for several models, makes the cost
`unavailable`, never partial.

**Reliability** reports the completion and tool error rates with failure breakdowns; an
absent outcome is never assumed successful. No calls means no rate: `skipped` (`no_calls`,
`no_tool_calls`), never `unavailable`. Every metric counts agent-scoped calls only
(`CallScope.agent`), so evaluation overhead never moves an agent's value.

## Metric keys

Keys are a versioned contract defined by this table (each row from the declared metadata):
changing a key breaks every persisted result.

| Key | Family / module | `rollup` | `direction` (better) |
|---|---|---|---|
| `stix.envelope_validity` | `STIX_BUNDLE` | `pooled` | higher |
| `stix.semantic_validity` | `STIX_BUNDLE` | `pooled` | higher |
| `stix.entity_prf` | `STIX_BUNDLE` | `pooled` | higher |
| `stix.entity_prf.by_type` | `STIX_BUNDLE` | `macro` | higher |
| `stix.relationship_prf` | `STIX_BUNDLE` | `pooled` | higher |
| `stix.entity_prf.aligned` | `STIX_BUNDLE` | `pooled` | higher |
| `stix.entity_prf.soft` | `STIX_BUNDLE` | `pooled` | higher |
| `stix.relationship_prf.aligned` | `STIX_BUNDLE` | `pooled` | higher |
| `stix.entity_prf.aligned.closed` | `STIX_BUNDLE` | `pooled` | higher |
| `stix.relationship_prf.aligned.closed` | `STIX_BUNDLE` | `pooled` | higher |
| `json.schema_validity` | `STRUCTURED_JSON` | `pooled` | higher |
| `json.field_accuracy` | `STRUCTURED_JSON` | `pooled` | higher |
| `classification.vocabulary_validity` | `CLASSIFICATION` | `pooled` | higher |
| `classification.micro_prf` | `CLASSIFICATION` | `pooled` | higher |
| `classification.macro_prf` | `CLASSIFICATION` | `macro` | higher |
| `classification.samples_f1` | `CLASSIFICATION` | `mean` | higher |
| `classification.subset_accuracy` | `CLASSIFICATION` | `pooled` | higher |
| `prose.exact_match` | `PROSE` | `pooled` | higher |
| `prose.token_prf` | `PROSE` | `mean` | higher |
| `prose.rouge_l` | `PROSE` | `mean` | higher |
| `prose.required_phrase_coverage` | `PROSE` | `mean` | higher |
| `prose.forbidden_phrase_absence` | `PROSE` | `mean` | higher |
| `performance.latency.wall_clock` | latency | `mean` | lower |
| `performance.latency.ttft` | latency | `mean` | lower |
| `performance.tokens.total` | tokens | `mean` | lower |
| `performance.cost.llm` | cost | `mean` | lower |
| `performance.counts.model_calls` | counts | `mean` | lower |
| `performance.counts.tool_calls` | counts | `mean` | lower |
| `performance.counts.retries` | counts | `mean` | lower |
| `performance.reliability.completion_rate` | reliability | `pooled` | higher |
| `performance.reliability.tool_error_rate` | reliability | `pooled` | lower |

The two phrase metrics are computed but hidden by the platform (`dev-docs/reference/evaluations.md` §14).

## Structure

Paths are relative to `backend/app/evaluation_metrics/`.

| Path | Purpose |
|------|---------|
| `core/contracts.py` | statuses, directions, roll-ups, `MetricError`, `check_rollup_counts` |
| `core/errors.py`, `core/serialization.py` | `ConfigurationError`; frozen mappings for byte-stable output |
| `quality/results.py`, `quality/unreadable.py` | `ScoreResult`, `MetricMetadata`; `unreadable_answer` |
| `quality/primitives/` | set diagnostics, P/R/F1, macro averaging, empty answer, pass/fail |
| `quality/normalization/` | `ComparisonPolicy`, `FieldMode`, prose normalization |
| `quality/families/<family>/` | one sub-package per family: `structured_json`, `classification`, `stix_bundle`, `prose` |
| `quality/families/routing.py` | `Family`, `FamilyConfig`, `run_for_family` |
| `quality/backends/` | STIX canonicalization and validation (pycti, stix2) |
| `performance/`, `performance/metrics/` | results, telemetry, units, pricing; the metrics |

## Development

    make test ARGS="tests/test_metric_suite_*.py"
    cd backend && uv run ruff check app/evaluation_metrics

Tests are flat in `backend/tests/`, named by subject: `test_metric_suite_*` for the package,
`test_evaluation_*` for the platform adapter.

### Boundary rules

1. **The package imports no platform code.** Nothing under `app/evaluation_metrics/` imports
   `app.services`, `app.models`, `app.api` or any other platform module.
   `test_no_suite_module_imports_platform_code` (`tests/test_metric_suite_contracts.py`)
   AST-scans every module, relative imports resolved. Inside, `quality` and `performance`
   import `core`, never each other.
2. **The platform reaches the package only through
   `app/services/evaluation/metric_package/`**, enforced by ruff's `TID251` banned-api rule
   and by `tests/test_evaluation_guards.py` (a `# noqa: TID251` silences the rule, not the
   test).

### How to add a metric

1. Declare its `MetricMetadata` or `PerformanceMetadata` as a module constant in its family's
   sub-package (or `performance/metrics/`), with a `rollup` and a `direction`; a `pooled` or
   `macro` result carries the counts `check_rollup_counts` requires.
2. Add its key to [Metric keys](#metric-keys).
3. Return it from the family's runner so `run_for_family` yields it.
4. Bump `__version__` with a docstring note.
5. Register nothing on the platform side: `metric_package/registry.py` walks the declared
   metadata. Only a new family needs an entry in `FAMILY_MODULES` and `families.FAMILY_IDS`.
6. Add it to `METRIC_KEY_LABELS`, with the package label verbatim, and to
   `METRIC_KEY_DESCRIPTIONS` (`frontend/src/lib/constants.ts`), pinned by
   `tests/test_evaluation_metric_catalog.py`; an entry view also needs `METRIC_ENTRY_LABELS`.
7. A quality metric is not a default contributor until added to `catalog.DEFAULT_QUALITY_KEYS`.
