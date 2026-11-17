# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Metric suite package metadata.

``__version__`` is the only version declaration for this suite. A second one (a
``pyproject.toml`` version, a build manifest) drifts: the manifest once said 2.1.0 while
the import reported 2.0.0.

**Bump it whenever a change alters what a metric computes.** It is recorded on every
evaluation run (``EvaluationRun.metric_suite_version``), so a score that moved because the
metric changed is not mistaken for a regression in the agent. A change that cannot alter a
score (docstring, test, annotation) needs no bump. When in doubt, bump.

2.2.0: the PROSE family scores. The suite code is unchanged; the platform adapter
supplies the ``ProseConfig``, and its normalization casefolds where the suite default keeps
case. The policy lives in ``app/services/evaluation/metric_package/quality_adapter.py``;
nothing ties it to this number, so ``tests/test_evaluation_metric_adapters.py`` pins its four
axes and its failure message points here.

3.0.0: breaking on two grounds, each enough for a major bump.

*Metric semantics.* ``json.field_accuracy`` scores each schema field in ``[0,1]`` under a mode
derived from that field's declaration and takes a weighted mean, instead of counting
flattened leaves as true/false positives and negatives. Every JSON score moves, in both
directions: a reordered but complete label set goes from 0.0 to 100.0, and a wrong field is
penalised once instead of twice. A well-formed but non-conforming answer reports what it got
right instead of 0.

*Removed API.* ``Family.JSON_ANALYSIS`` and ``Family.DETECTION_RULE`` are folded into
``STRUCTURED_JSON``, whose per-field mechanism computes the same thing. The ``quality.attack``
module goes with them: a field of ATT&CK identifiers is scored by the ``enum_set`` mode.

4.0.0: breaking, because ``performance.latency.wall_clock`` measures something else.
It was the sum of the agent's recorded steps, missing the gaps, a blocking park and, on an
empty timeline such as a transfer, the whole turn (0 ms, a perfect score). The engine now
times the agent call. The suite code is unchanged; its input is not. The figure is elapsed
time to the answer, including an ``llm_only`` fallback after a failed ``full_loop`` and
contention with concurrent runs, so it is always at least the old one and ``wall_clock`` and
Overall Performance are not comparable across this boundary. A reused prediction recorded
before the clock existed reports latency ``unavailable``.

Same release: an entry ending on a context overflow or an HTTP 413 keeps the steps recorded
before the error, so its tokens, cost and model calls score instead of ``unavailable``. A 413
counts as a failed call; an overflow is caught before any request and adds no call.

5.0.0: breaking, because tokens, cost and call counts measure something else. Rebuilt
from the evaluated agent's progress events, they lost a handover's work (a consulted or
transferred agent's calls were overwritten or mixed in; a delegated agent sends no events)
and the agent's own silent calls (a continuation after a length cut, a compaction summary, a
forced final answer). They are counted from each agent's own trace, summed over the chain of
agents an entry involved, each model priced at its own price
(``ExecutionTelemetry.agent_token_usage_by_model``, ``estimated_llm_cost(prices_by_model=...)``).
Per-call durations come from each call's trace. Only an entry with no handover and no such
call is comparable across this boundary.

6.0.0: breaking, because the performance metric set goes from 19 to 9. Removed: the
six metrics no run computed (``latency.component_sum``, ``latency.model_calls``,
``latency.tool_calls``, the three ``overhead.*``), ``counts.iterations`` (same as
``counts.model_calls``), and the ``error``, ``timeout`` and ``cancelled`` rates, which moved
with ``completion_rate``. Malformed telemetry marks all nine metrics ``invalid``. Also records
a change shipped without a bump: Full Loop measures time to first token, and LLM Only its
tokens and cost. Overall Performance is not comparable across this boundary.

7.0.0: breaking on two grounds. Every metric's metadata declares how a run combines
its entry values (``rollup``: ``mean`` or ``pooled``), a required field. The two reliability
rates are ``pooled``: a scored result carries its counts in ``detail`` (``numerator``,
``denominator``) and the run adds them up instead of averaging rates, with no spread penalty.
An entry's rate is unchanged; a run's ``completion_rate`` and ``tool_error_rate`` move when
its entries hold different numbers of calls (10 calls with 5 failures plus 1 call with none:
5 / 11 = 0.455, where the penalised mean read 0.375). Every quality metric is ``mean``.

8.0.0: breaking. The quality set goes from 20 metrics to 17: ``json.syntactic_validity``
(``json.schema_validity``'s reason already tells "not JSON" from "schema violation"),
``classification.hamming_loss`` (near-constant, depends on the vocabulary size) and
``classification.example.set_prf`` (never routed) are removed. ``classification_dataset``
loses ``macro_universe``: macro averaging always runs over the labels seen on either side.
``run_for_family`` raises ``ConfigurationError`` for ``PROSE`` without a ``ProseConfig``
instead of returning the ``family.unavailable`` sentinel, which is removed. Public helpers
with no caller are removed: ``positional_diagnostics``, ``micro_average``,
``per_label_scores``, the ``sort_key`` parameter of the set diagnostics, the ``universe``
parameter of ``macro_average``, ``performance.PerformanceConfig`` and the ``Metric``
protocol. ``json.field_accuracy`` declares ``kind="accuracy"``. No value of a remaining
metric changes, but the platform's default contributors to Overall Quality change with
this release: an unconfigured Overall Quality is not comparable across this
boundary.

9.0.0: breaking, because the prose and classification metrics score something
else. ``PunctuationMode.edges`` trims the marks that present a token (sentence punctuation
at its end, brackets and quotes, ``*`` and backtick emphasis), never the characters that
identify it. The platform's prose policy uses it and its CLASSIFICATION labels are
case-folded and trimmed the same way, so its prose metrics, the ``free_text`` fields of
``json.field_accuracy`` and its classification metrics are not comparable across this
boundary. ``quality.trim_edge_marks`` applies the rule to one token.

10.0.0: breaking, because ``json.field_accuracy`` scores something else. Leaves
count, each once: a nested object and each entry of a list of objects count as their
leaves, whether the answer holds them or not, and list entries are paired by optimal
assignment instead of greedily, so their order no longer moves the score. An omitted field
is a miss whether ``required`` or not, an empty value on one side only is left out, an
array of plain values is a set (F1) and a tuple ``exact``, a nullable type keeps the mode
of its non-null type, and set values compare in their JSON form. ``MAX_PAIRS_SCORED``
bounds the pairs one answer may score. JSON scores are not comparable across this
boundary.

11.0.0: breaking, because an entry with nothing expected and nothing given scores
100 in every family (STIX entity and relationship F1, ``{}`` against ``{}`` and an empty
``free_text`` field in ``json.field_accuracy``), and a pooled quality result may carry 0 of
0 at 100. An empty PROSE reference is skipped (``reference_empty``), a STIX type no
implementation knows is identified by its content instead of raising, and a list over the
pairing bound scores 0 when the reference paired with itself fits. ``empty_answer`` moves
to ``quality.primitives``.

12.0.0: breaking, because STIX objects are identified by what they say: values are
normalized first (timestamps, relationship types, domain names, hashes, names, indicator
patterns), the official identifiers never see the authoring metadata and are computed after
the references they hold, an object no implementation identifies gets a content identifier
instead of crashing the entry, and objects sharing an id are versions, the latest kept.
``stix.relationship_prf`` counts sightings. STIX values are not comparable across this
boundary.

12.1.0: an agent that made no call has no reliability rate: ``completion_rate`` is
``skipped`` (``no_calls``) and ``tool_error_rate`` ``skipped`` (``no_tool_calls``), where
both read ``unavailable`` (``outcome_not_captured``), as if the telemetry were missing. No
value changes: neither status enters an aggregate.

13.0.0: breaking, because a run combines most quality metrics differently. Each
metric declares the roll-up its definition calls for: ``stix.entity_prf``,
``stix.relationship_prf`` and ``classification.micro_prf`` are ``pooled`` (``2TP`` of
``2TP + FP + FN``), ``json.field_accuracy`` is ``pooled`` over its fields' credit and
weight, and every pass/fail metric (the validity checks, ``classification.subset_accuracy``,
``prose.exact_match``) is ``pooled`` over one item per entry, so a run reports its share of
passes with no spread penalty. ``classification.macro_prf`` and ``stix.entity_prf.by_type``
are ``macro``, a new roll-up: each result carries its per-class counts in
``detail["classes"]`` (formerly ``per_label`` and ``per_type``) and a run averages the F1
of each class summed over its entries. Pooled counts may be non-negative reals
(``check_rollup_counts``, formerly ``check_pooled_counts``). An answer a metric cannot read
counts as the expected answer would (``quality.unreadable_answer``). ``classification_dataset``
skips every metric (``no_examples``) on an empty collection instead of scoring it 0. An
entry's value is unchanged; a run's is not comparable across this boundary.

13.1.0: a CLASSIFICATION answer that is one Markdown code block is read as what the
block holds, not as its labels plus the lines of its fence (``json``, a backtick), and a fence
line is never a label. An entry the agent answered right inside a fence scored TP 3 and FP 2,
and now scores TP 3 and FP 0; a fenced reference no longer adds fence labels to the derived
vocabulary. The suite code is unchanged: the platform's ``parse_labels`` is. An unfenced
answer scores the same, so only a run of an agent that fences its answers is not comparable
across this boundary.

13.2.0: ``json.field_accuracy`` descends at most ``MAX_FIELD_DEPTH`` (64) levels.
An answer nested deeper is ``skipped`` (``nesting_too_deep``, with the depth reached and
the bound) instead of raising ``RecursionError`` from about 500 levels, which failed the
entry as the agent's and lost its answer and its ``json.schema_validity``. Only an answer
nested past 64 levels scores differently.

14.0.0: breaking, because ``stix.semantic_validity`` judges the bundle as written.
An object missing ``id``, or an SDO / SRO missing ``spec_version``, ``created`` or
``modified``, scored 100 because ``stix2.parse`` fills them in; it now scores 0 with a
``missing_required_property`` finding naming the object and the property, and a
``modified`` earlier than ``created`` with ``modified_before_created``. A complete bundle
scores as before; an incomplete one is not comparable across this boundary.

15.0.0: breaking, because ``json.field_accuracy`` reads the schema a pydantic model
generates. A field's declaration is read through its local ``$ref`` (``#/$defs/...``),
where it scored ``exact`` whatever it declared; a ``const`` field is an ``enum``, where it
was ``free_text``; and a list whose ``items`` are an ``anyOf`` / ``oneOf`` of objects is an
``object_list``, where it was one ``enum_set`` field compared whole: each pair is scored
field by field under the branch its reference entry matches, read from the values the
branches fix differently (``const`` / ``enum``), else the properties every branch declares
alike. An object field of several shapes takes its branch the same way. A schema with
neither scores as before; any other answer is not comparable across this boundary.
"""

__version__ = "15.0.0"
