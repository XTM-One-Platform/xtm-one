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

// The compare bar of a run review: B, the run under review, against A, its reference (the
// expected bundles, nothing, or another run of the evaluation), the quick comparisons a
// reviewer reaches for, and the what-if.

import { useId } from "react";
import { Icon, IconButton } from "@filigran/design-system";
import { SearchableSelect, type SelectOption } from "@/components/ui/searchable-select";
import { Switch } from "@/components/ui/switch";
import { useFormatter } from "@/i18n";
import { EXPECTED, NONE, formatScore } from "@/lib/evaluation-bundle-review";
import { launchesOf } from "@/lib/evaluation-leaderboard";
import type { Preset } from "@/lib/evaluation-run-review";
import type { ComparedRun } from "@/types";

export interface RunNames {
  /** A run's name: its agent or model, and when it started. */
  name: (run: ComparedRun) => string;
  /** Its agent or model alone (its configuration). */
  config: (run: ComparedRun) => string;
  /** When it started. */
  code: (run: ComparedRun) => string;
  /** A launch's name: when it started and what started it. */
  launch: (time: string, trigger: string | null) => string;
}

function presetLabel(p: Preset, names: RunNames, t_i18n: (id: string, v?: Record<string, string | number>) => string): string {
  const [a, b] = p.runs;
  const config = names.config(a);
  switch (p.kind) {
    case "first-vs-expected":
    case "latest-vs-expected":
      return t_i18n("{config} {run} vs the expected bundles", { config, run: names.code(a) });
    case "first-to-latest":
    case "previous-to-latest":
      return t_i18n("{config} {from} → {to}", { config, from: names.code(a), to: names.code(b) });
    case "rerun":
      return t_i18n("{config} {run} vs its rerun {rerun} (variance)", { config, run: names.code(a), rerun: names.code(b) });
    case "best-other":
      return t_i18n("{config} vs {other} ({run})", { config, other: names.config(b), run: names.code(a) });
    case "latest-alone":
      return t_i18n("{config} {run} alone", { config, run: names.code(a) });
  }
}

export function RunPicker({
  runs,
  under,
  against,
  figure,
  presets,
  names,
  whatIf,
  onPair,
  onWhatIf,
}: {
  runs: ComparedRun[];
  under: ComparedRun;
  /** `expected`, `none` or a run id. */
  against: string;
  /** A run's Entity F1, for its option. */
  figure: (runId: string) => number | null;
  presets: Preset[];
  names: RunNames;
  whatIf: boolean;
  onPair: (ref: string, under: string) => void;
  onWhatIf: (on: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const switchId = useId();
  const launches = [...launchesOf(runs)].reverse();
  const runOptions = (disabled: string | null): SelectOption[] =>
    launches.flatMap((launch) =>
      launch.runs.map((run) => ({
        value: run.id,
        label: `${names.config(run)} · ${names.code(run)} · ${t_i18n("F1 {value}", { value: formatScore(figure(run.id)) })}`,
        group: names.launch(launch.time, launch.trigger),
        disabled: run.id === disabled,
      })),
    );
  const isRun = against !== EXPECTED && against !== NONE;
  const ref = isRun ? runs.find((r) => r.id === against) : undefined;
  const current = presets.findIndex((p) => p.ref === against && p.under === under.id);
  const what = !isRun
    ? against === NONE
      ? t_i18n("{answer} alone: what it found, checked against the expected bundle.", { answer: `B · ${names.name(under)}` })
      : t_i18n("{answer} checked against the expected bundle.", { answer: `B · ${names.name(under)}` })
    : t_i18n("What changed from {a} to {b}, each judged against the expected bundle.", { a: `A · ${ref ? names.name(ref) : ""}`, b: `B · ${names.name(under)}` });
  return (
    <div className="space-y-3" role="group" aria-label={t_i18n("What to compare")}>
      <div className="flex flex-wrap items-end gap-x-4 gap-y-3">
        <div className="min-w-64 flex-1 space-y-1">
          <p className="content-caption text-default-secondary">
            <span className="mr-1 font-semibold text-default-primary">B</span>
            {t_i18n("Run")}
          </p>
          <SearchableSelect aria-label={t_i18n("Run under review")} sort={false} value={under.id} options={runOptions(isRun ? against : null)} onValueChange={(id) => onPair(against, id)} />
        </div>
        <span className="pb-2 content-compact text-default-secondary">{t_i18n("vs")}</span>
        <div className="min-w-64 flex-1 space-y-1">
          <p className="content-caption text-default-secondary">
            <span className="mr-1 font-semibold text-default-primary">A</span>
            {t_i18n("Reference")}
          </p>
          <SearchableSelect
            aria-label={t_i18n("Reference")}
            sort={false}
            value={against}
            pinned={[
              { value: EXPECTED, label: t_i18n("Expected (dataset)") },
              { value: NONE, label: t_i18n("Nothing: the run alone") },
            ]}
            options={runOptions(under.id)}
            onValueChange={(id) => onPair(id, under.id)}
          />
        </div>
        <IconButton
          size="sm"
          aria-label={t_i18n("Swap A and B")}
          title={t_i18n("Swap A and B")}
          disabled={!isRun}
          icon={<Icon name="arrow-right-left" size={16} />}
          onClick={() => onPair(under.id, against)}
        />
      </div>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
        {presets.length > 0 && (
          <div className="min-w-64">
            <SearchableSelect
              aria-label={t_i18n("Quick comparisons")}
              placeholder={t_i18n("Quick comparisons…")}
              sort={false}
              searchable="auto"
              value={current >= 0 ? String(current) : null}
              options={presets.map((p, i) => ({ value: String(i), label: presetLabel(p, names, t_i18n) }))}
              onValueChange={(v) => {
                const p = presets[Number(v)];
                if (p) onPair(p.ref, p.under);
              }}
            />
          </div>
        )}
        <label htmlFor={switchId} className="flex items-center gap-2 content-compact">
          <Switch id={switchId} checked={whatIf} onCheckedChange={onWhatIf} />
          {t_i18n("Count near-misses as matches (what-if)")}
        </label>
      </div>
      <p className="content-compact">{what}</p>
    </div>
  );
}
