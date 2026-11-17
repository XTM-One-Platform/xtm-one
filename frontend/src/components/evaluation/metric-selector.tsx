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

// The overall score composition control, shared by the creation screen and the
// Configuration tab's Scoring card, so both offer the same choice the same way.

import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { InfoLabel } from "@/components/ui/info-tip";
import { HEADLINE_LABELS, HEADLINE_NOTES } from "@/lib/evaluation-headlines";
import { useFormatter } from "@/i18n";
import { toggleArrayItem } from "@/lib/utils";
import type { MetricCatalogEntry } from "@/types";
import { MetricName } from "./metric-name";

/**
 * One headline's metric composition, as a tri-state control. The three
 * states must stay visibly apart:
 *
 * - **inherit all** (`null`) — every metric the family measures composes the
 *   headline, including metrics added upstream later.
 * - **select none** (`[]`) — nothing composes it, so the headline reads N/A.
 * - **a subset** — exactly the named metrics compose it.
 *
 * "Select none" and "unconfigured" must never look alike, or an author who cleared
 * a headline on purpose would read the resulting N/A as a defect.
 */
function MetricSelector({
  title,
  note,
  kind,
  catalog,
  value,
  onChange,
}: {
  title: string;
  /** What the overall score is, a catalog key (`HEADLINE_NOTES`): the title's tooltip. */
  note: string;
  kind: "quality" | "performance";
  catalog: MetricCatalogEntry[];
  value: string[] | null;
  onChange: (next: string[] | null) => void;
}) {
  const { t_i18n } = useFormatter();
  const available = catalog.filter((m) => m.kind === kind);
  // A performance metric with no declared direction cannot be normalised, so
  // offering it would let an author compose a headline that silently drops it.
  const selectable = available.filter(
    (m) => kind === "quality" || m.direction !== null,
  );
  // What "use defaults" composes: the default contributors, not every metric. Manual
  // mode is seeded from these so switching modes opts into no non-default metric.
  const defaults = selectable.filter((m) => m.default_contributor);
  const optIn = selectable.length - defaults.length;
  const inheritAll = value === null;

  return (
    <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="content-base font-medium">
            <InfoLabel content={t_i18n(note)}>{title}</InfoLabel>
          </p>
          <p className="text-[11px] text-default-secondary mt-0.5">
            {inheritAll
              ? optIn > 0
                ? t_i18n("Composed from this family's default metrics, including any added later. {count, plural, one {# metric is} other {# metrics are}} not counted by default.", {
                    count: optIn,
                  })
                : t_i18n("Composed from every metric this family measures, including any added later.")
              : value.length === 0
                ? t_i18n("Composed from nothing: this overall score will read N/A.")
                : t_i18n("{count} of {total, plural, one {# metric} other {# metrics}} selected", {
                    count: value.length,
                    total: selectable.length,
                  })}
          </p>
        </div>
        <Tip content={t_i18n("Off lets you pick individual metrics. Turning it back on restores this family's default metrics.")}>
          <span className="flex items-center gap-2 shrink-0">
            <span className="text-[11px] text-default-secondary">{t_i18n("Use defaults")}</span>
            <Switch aria-label={t_i18n("Use defaults")}
              checked={inheritAll}
              // Nothing to seed from yet (the catalog is loading, or failed): switching
              // off now would compose the score from nothing.
              disabled={inheritAll && selectable.length === 0}
              onCheckedChange={(on) => onChange(on ? null : defaults.map((m) => m.key))}
            />
          </span>
        </Tip>
      </div>

      {!inheritAll && (
        selectable.length === 0 ? (
          <p className="text-[11px] text-default-secondary italic">
            {t_i18n("This family measures no metric of this kind yet.")}
          </p>
        ) : (
          <div className="space-y-1.5 max-h-56 overflow-y-auto pr-1">
            {selectable.map((m) => (
              <div key={m.key} className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="content-compact truncate flex items-center gap-1.5">
                    {/* The run page's name for the metric, so a metric is called one thing
                        everywhere; the backend's label only for a key the table does
                        not name. */}
                    <MetricName metricKey={m.key} fallback={m.label} className="truncate" />
                    {m.default_contributor === false && (
                      <InfoLabel content={t_i18n("Not part of the default: select it deliberately to include it in this score.")}>
                        <Badge variant="outline" className="text-[9px] px-1 py-0 font-normal text-default-secondary">
                          {t_i18n("not counted by default")}
                        </Badge>
                      </InfoLabel>
                    )}
                  </p>
                  <p className="content-caption font-mono-plex text-default-secondary truncate">{m.key}</p>
                </div>
                <Switch aria-label={m.key}
                  checked={value.includes(m.key)}
                  onCheckedChange={() => onChange(toggleArrayItem(value, m.key))}
                />
              </div>
            ))}
          </div>
        )
      )}

      {!inheritAll && available.length > selectable.length && (
        <p className="content-caption text-default-secondary">
          {t_i18n("{count, plural, one {# metric} other {# metrics}} hidden: the package declares no direction for them, so they cannot be normalised", {
            count: available.length - selectable.length,
          })}
        </p>
      )}
    </div>
  );
}

/**
 * Which metrics compose Overall Quality and Overall Performance: the Scoring card's
 * `aggregationExtra` on the Configuration tab and on the creation screen alike.
 */
export function OverallScoreComposition({
  catalog,
  quality,
  performance,
  onQualityChange,
  onPerformanceChange,
}: {
  catalog: MetricCatalogEntry[];
  quality: string[] | null;
  performance: string[] | null;
  onQualityChange: (next: string[] | null) => void;
  onPerformanceChange: (next: string[] | null) => void;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-4 pt-3 border-t border-elevation-subtle-soft">
      <div>
        <p className="content-base font-medium">{t_i18n("Overall Score Composition")}</p>
        <p className="text-[11px] text-default-secondary mt-0.5">
          {t_i18n("Choose which metrics compose Overall Quality and Overall Performance. Every metric is still measured and stored either way: this only governs what the overall score is made of, so narrowing one is always reversible.")}
        </p>
      </div>
      <MetricSelector
        title={t_i18n(HEADLINE_LABELS.quality)}
        note={HEADLINE_NOTES.quality}
        kind="quality"
        catalog={catalog}
        value={quality}
        onChange={onQualityChange}
      />
      <MetricSelector
        title={t_i18n(HEADLINE_LABELS.performance)}
        note={HEADLINE_NOTES.performance}
        kind="performance"
        catalog={catalog}
        value={performance}
        onChange={onPerformanceChange}
      />
    </div>
  );
}
