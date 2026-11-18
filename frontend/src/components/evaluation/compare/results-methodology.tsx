/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The methodology of one evaluation's Results (#2): what every figure on the page means,
 * to publish next to it, as a definition list and as the Markdown it exports. A family
 * whose scorer the page knows (STIX bundles) has its own lines; every other family the
 * generic ones.
 */

import { useState } from "react";
import { useFormatter } from "@/i18n";
import { SAMPLES, type BoardRun } from "@/lib/evaluation-leaderboard";
import { expectedTotal, suiteVersions } from "@/lib/evaluation-results";
import type { ComparedCell, ComparedEvaluation, ComparedMetric } from "@/types";

export interface MethodItem {
  term: string;
  text: string;
}

const STIX = {
  family: "STIX_BUNDLE",
  objects: "stix.entity_prf",
  relationships: "stix.relationship_prf",
  bundle: "stix.bundle_f1",
} as const;

interface Input {
  evaluationName: string;
  evaluation?: ComparedEvaluation;
  runs: BoardRun[];
  cells: Map<string, Map<string, ComparedCell>>;
  metrics: ComparedMetric[];
  entryCount: number;
}

/** The methodology's lines, translated. */
export function useMethodology({ evaluationName, evaluation, runs, cells, metrics, entryCount }: Input): MethodItem[] {
  const { t_i18n, intl } = useFormatter();
  // When the page read the figures: once per visit, so the text never moves while open.
  const [readAt] = useState(() => new Date().toISOString());
  const today = intl.formatDate(readAt, { dateStyle: "medium" });
  const has = (key: string) => metrics.some((m) => m.key === key);
  const stix = evaluation?.family === STIX.family || (evaluation == null && has(STIX.bundle));
  const objects = stix ? expectedTotal(cells, STIX.objects) : null;
  const relationships = stix ? expectedTotal(cells, STIX.relationships) : null;
  const items: MethodItem[] = [];

  // Dataset
  const datasets = evaluation?.datasets ?? [];
  const notes = datasets.map((d) => d.description?.trim()).filter(Boolean).join(" ");
  const entries = t_i18n("{count, plural, =0 {no entry} one {# entry} other {# entries}}", { count: entryCount });
  const totals =
    objects != null && relationships != null
      ? t_i18n("{entries}, {objects} expected STIX objects and {relationships} expected relationships", { entries, objects, relationships })
      : entries;
  items.push({
    term: t_i18n("Dataset"),
    text: [
      datasets.length
        ? t_i18n("“{names}”: {totals}.", { names: datasets.map((d) => d.name).join("”, “"), totals })
        : t_i18n("{totals} of the evaluation's datasets.", { totals }),
      notes,
    ].filter(Boolean).join(" "),
  });

  // Task
  if (evaluation) {
    const mode = evaluation.execution_mode ?? runs[0]?.run.execution_mode ?? "—";
    const s = evaluation.timeout_seconds;
    const timeout = s == null ? "—" : s >= 3600 ? `${Math.round((s / 3600) * 10) / 10} h` : `${Math.round(s / 60)} min`;
    items.push({
      term: t_i18n("Task"),
      text: stix
        ? t_i18n("The agent reads one entry and returns a STIX 2.1 bundle; full agentic loop with tools ({mode}), timeout {timeout} per run.", { mode, timeout })
        : t_i18n("The agent answers each entry through its agentic loop ({mode}), timeout {timeout} per run.", { mode, timeout }),
    });
  }

  // Figures
  if (stix && has(STIX.bundle) && objects != null && relationships != null) {
    items.push({
      term: t_i18n("Bundle F1"),
      text: t_i18n("The headline figure: one micro F1 over everything the bundle should contain, the {objects} objects and the {relationships} relationships counted together (true positives, spurious and missed items of both summed, then one F1). Relationships weigh by their number ({share}% of the items here). Computed from the platform's entity and relationship counts.", {
        objects, relationships, share: objects + relationships ? Math.round((100 * relationships) / (objects + relationships)) : 0,
      }),
    });
  }
  if (stix && has(STIX.objects)) {
    items.push({
      term: t_i18n("Entity F1"),
      text: t_i18n("Micro F1 over canonical object IDs: every object of the answer and of the dataset gets its OpenCTI deterministic ID (pycti), so a different name, alias, defanged value or type is a miss plus a spurious object. Metric suite {suites}.", {
        suites: suiteVersions(runs).join(", ") || "—",
      }),
    });
  }
  if (stix && has(STIX.relationships)) {
    items.push({
      term: t_i18n("Relationship F1"),
      text: t_i18n("Micro F1 over canonical (source, type, target) triples; a sighting counts as one triple per sighted location. An entity miss also breaks its relationships."),
    });
  }
  const explained = new Set<string>(stix ? [STIX.bundle, STIX.objects, STIX.relationships] : []);
  const others = metrics.filter((m) => !explained.has(m.key));
  if (others.length) {
    items.push({
      term: stix ? t_i18n("Other figures") : t_i18n("Figures"),
      text: t_i18n("{metrics}: each pooled, its counts added up over the entries then divided once (micro), as the run's own headline; an entry with nothing expected and nothing found counts as correct.", {
        metrics: others.map((m) => t_i18n(m.label)).join(", "),
      }),
    });
  }

  items.push({
    term: t_i18n("Unreadable answers"),
    text: stix
      ? t_i18n("An answer that is not a readable STIX bundle counts as every expected object and relationship missing. A platform error or a judge failure leaves the entry out of that run's figures, as the run's own headline does.")
      : t_i18n("An answer the scorer cannot read counts as everything expected missing. A platform error or a judge failure leaves the entry out of that run's figures, as the run's own headline does."),
  });

  // Judge
  if (!evaluation) {
    items.push({ term: t_i18n("Judge score"), text: t_i18n("Not used here: every figure comes from the deterministic metrics.") });
  } else if (!evaluation.judge_on) {
    items.push({ term: t_i18n("Judge score"), text: t_i18n("The judge was off for this evaluation.") });
  } else {
    const weights = Object.entries(evaluation.judge_weights ?? {});
    const total = weights.reduce((s, [, v]) => s + v, 0) || 1;
    items.push({
      term: t_i18n("Judge score"),
      text: t_i18n("LLM judge, {model}; weights {weights}. Not used for the F1 figures.", {
        model: evaluation.evaluator_model || t_i18n("platform default model (no evaluator model set)"),
        weights: weights.map(([k, v]) => `${k.replace(/_/g, " ")} ${Math.round((100 * v) / total)}%`).join(", ") || "—",
      }),
    });
  }

  items.push(
    {
      term: t_i18n("Uncertainty"),
      text: t_i18n("95% percentile bootstrap, {samples} resamples of the entries, and of the runs when a configuration has several, fixed seed. Ranks are shown as best–worst (as LMArena does): two configurations whose intervals overlap are tied. With {entries} these intervals are optimistic; a ranking claim needs a larger dataset.", {
        samples: SAMPLES, entries,
      }),
    },
    {
      term: t_i18n("Cost and time"),
      text: t_i18n("LLM cost reported by the platform per answer, summed and divided by the answers that have one; no adjustment for prompt caching. Configurations with no reported cost are left out of the cost chart. Time is the agent's seconds per entry."),
    },
    {
      term: t_i18n("Runs"),
      text: t_i18n("{count, plural, =0 {No completed run} one {The # completed run} other {The # completed runs}} of “{name}” (its 50 newest at most); placebo comparison runs are left out. A launch is the runs started in the same minute by the same trigger.", {
        count: runs.length, name: evaluationName,
      }),
    },
    {
      term: t_i18n("Provenance"),
      text: evaluation?.created_at
        ? t_i18n("Figures read on {today} from the stored scores of “{name}”, created on {created}.", {
            today, name: evaluationName, created: intl.formatDate(evaluation.created_at, { dateStyle: "medium" }),
          })
        : t_i18n("Figures read on {today} from the stored scores of “{name}”.", { today, name: evaluationName }),
    },
  );
  return items;
}

export function MethodologyList({ items }: { items: MethodItem[] }) {
  return (
    <dl className="grid gap-x-6 gap-y-2 content-compact sm:grid-cols-[12rem_1fr]">
      {items.map((it) => (
        <div key={it.term} className="contents">
          <dt className="font-medium">{it.term}</dt>
          <dd className="text-default-secondary">{it.text}</dd>
        </div>
      ))}
    </dl>
  );
}
