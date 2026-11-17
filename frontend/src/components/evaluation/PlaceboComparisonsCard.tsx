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

import { useState } from "react";
import { AlertTriangle, FlaskConical } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { InfoLabel, InfoTip } from "@/components/ui/info-tip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { placeboWarningEffect } from "./placebo-warning-effects";
import { usePlaceboComparisons } from "@/hooks/api/useEvaluationApi";
import { useSkills } from "@/hooks/api/useSkillApi";
import { useFormatter } from "@/i18n";
import { EVALUATION_STATUS_CONFIG } from "@/lib/constants";
import { cn, formatShortDate, timeAgo } from "@/lib/utils";
import {
  PLACEBO_TARGET_LABELS,
  PLACEBO_VERDICT_BADGES,
  PLACEBO_VERDICT_LABELS,
  formatDifference,
  formatRatio,
  formatShare,
  placeboWarningLabel,
  shortCorpusVersion,
  targetFromKey,
} from "@/lib/placebo";
import type { PlaceboComparison, PlaceboComparisonRow, PlaceboPairStatistics, PlaceboTarget, PlaceboVerdict } from "@/types";
import { Spinner } from "@filigran/design-system";

/** The rule behind each verdict of the real agent against its placebo (FR-26,
 * `placebo_stats.verdict`), read by `t_i18n`. @i18n-keys */
const PLACEBO_VERDICT_RULES = {
  beats_placebo: "At least one outcome improves and none worsens: higher quality or fewer tokens than the placebo, beyond chance.",
  trade_off: "One outcome improves and the other worsens: for example higher quality for more tokens.",
  worse_than_placebo: "At least one outcome worsens and none improves: lower quality or more tokens than the placebo, beyond chance.",
  no_better_than_placebo: "Neither quality nor tokens differ from the placebo beyond chance: the target shows no measurable effect.",
  not_computable: "No verdict: a run of the comparison did not complete, fewer than 3 entries were counted, or quality was not measured.",
} as const satisfies Record<PlaceboVerdict, string>;

/**
 * The Overview's placebo comparison block (FR-29): one row per agent x model x
 * target with the quality difference `D` and token ratio `R` of the real agent
 * against its placebo, their intervals, the verdict, each arm against the
 * no-target arm, the trial counts, how often a skill target was opened, and the
 * warnings. Nothing while the evaluation has never run a comparison.
 */
export function PlaceboComparisonsCard({ evaluationId }: { evaluationId: string }) {
  const { t_i18n } = useFormatter();
  const { data: comparisons = [] } = usePlaceboComparisons(evaluationId);
  const { data: skills = [] } = useSkills({ scope: "all" });
  const [chosen, setChosen] = useState<string | null>(null);
  if (comparisons.length === 0) return null;
  const comparison = comparisons.find((c) => c.id === chosen) ?? comparisons[0];

  const targetLabel = (target: PlaceboTarget) =>
    target.kind === "skill"
      ? `${t_i18n(PLACEBO_TARGET_LABELS.skill)} · ${skills.find((skill) => skill.id === target.skill_id)?.name ?? target.skill_id}`
      : t_i18n(PLACEBO_TARGET_LABELS[target.kind] ?? target.kind);
  const warningText = (code: string) => {
    const label = placeboWarningLabel(code);
    return label.known ? t_i18n(label.key) : label.key;
  };

  return (
    <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-base font-medium">
            <FlaskConical className="h-4 w-4 text-filigran-ia-primary" />
            {t_i18n("Placebo comparison")}
            <InfoTip
              label={t_i18n("What a placebo comparison is")}
              content={t_i18n(
                "Each agent runs next to copies of itself whose target is replaced by a neutral text of the same length (placebo) or removed (no target), to tell the target's own effect from the effect of any text of that length.",
              )}
            />
          </CardTitle>
          {comparisons.length > 1 && (
            <div className="w-64">
              <SearchableSelect
                aria-label={t_i18n("Placebo comparison")}
                options={comparisons.map((c) => ({ value: c.id, label: `${formatShortDate(c.created_at)} · ${statusText(t_i18n, c.status)}` }))}
                value={comparison.id}
                onValueChange={setChosen}
                searchable={false}
              />
            </div>
          )}
        </div>
        <p className="content-caption text-default-secondary">
          {t_i18n("Started {time}", { time: timeAgo(comparison.created_at) })}
          {" · "}
          <InfoLabel
            content={t_i18n(
              "The value that chose the placebos' neutral passages, the order the arms ran in and the resampling behind the intervals, so the comparison can be rebuilt.",
            )}
          >
            {t_i18n("seed {seed}", { seed: comparison.seed })}
          </InfoLabel>
          {comparison.corpus_version && (
            <>
              {" · "}
              <InfoLabel content={t_i18n("The version of the neutral text corpus the placebos were drawn from.")}>
                {t_i18n("corpus {version}", { version: shortCorpusVersion(comparison.corpus_version) })}
              </InfoLabel>
            </>
          )}
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        <ComparisonStatus comparison={comparison} />

        {comparison.warnings.length > 0 && (
          <ul className="space-y-0.5 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2">
            {comparison.warnings.map((warning, index) => (
              <li key={index} className="flex items-start gap-1.5 content-compact text-default-secondary">
                <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-feedback-warning-primary" />
                <WarningText code={warning.code}>
                  {warningText(warning.code)}
                  {warning.code === "target_skipped" && warning.agent_name
                    ? ` - ${warning.agent_name} · ${warning.target ? targetLabel(targetFromKey(warning.target)) : ""}: ${warning.reason ?? ""}`
                    : ""}
                </WarningText>
              </li>
            ))}
          </ul>
        )}

        {comparison.results && comparison.results.rows.length > 0 && (
          <>
            <DataTable
              rows={comparison.results.rows}
              columns={comparisonColumns(
                t_i18n,
                targetLabel,
                warningText,
                comparison.results.method.alpha,
                comparison.results.rows.some((row) => row.arms.real.skill_loaded !== null),
              )}
              rowKey={(row) => `${row.agent_id}/${row.model_id}/${row.target_key}`}
              searchFields={(row) => [row.agent_name, row.model_id, targetLabel(row.target)]}
              searchPlaceholder={t_i18n("Search agents, models and targets")}
              filters={[
                { key: "verdict", label: "Verdict", values: (row) => [t_i18n(PLACEBO_VERDICT_LABELS[row.verdict in PLACEBO_VERDICT_LABELS ? row.verdict : "not_computable"])] },
                { key: "target", label: "Target", values: (row) => [targetLabel(row.target)] },
              ]}
              exportName="placebo-comparison"
              urlKey="pc_"
              storageKey="placebo-comparisons"
              rowLines={2}
              layout="fixed"
              emptyLabel={t_i18n("No results")}
            />
            <p className="content-caption text-default-secondary">
              {t_i18n(
                "D: Overall Quality points of the real agent minus its placebo. R: the real agent's tokens over its placebo's, below 1 it is cheaper. 95 % intervals from {resamples, number} bootstrap resamples over the entries; verdicts read Holm-adjusted p-values below {alpha}.",
                { resamples: comparison.results.method.resamples, alpha: comparison.results.method.alpha },
              )}
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}

/** A warning's text, with what it changes in how the comparison reads as its tooltip when known. */
function WarningText({ code, children }: { code: string; children: React.ReactNode }) {
  const { t_i18n } = useFormatter();
  const effect = placeboWarningEffect(code);
  if (!effect) return <span>{children}</span>;
  return <InfoLabel content={t_i18n(effect)}>{children}</InfoLabel>;
}

/** A comparison's status, worded like its runs' (`EVALUATION_STATUS_CONFIG`); a status it does not know as is. */
function statusText(t_i18n: (id: string) => string, status: string): string {
  const label = EVALUATION_STATUS_CONFIG[status]?.label;
  return label ? t_i18n(label) : status;
}

function ComparisonStatus({ comparison }: { comparison: PlaceboComparison }) {
  const { t_i18n } = useFormatter();
  if (comparison.status === "pending" || comparison.status === "running") {
    const finished = comparison.runs.filter((run) => !["pending", "running"].includes(run.status)).length;
    return (
      <p className="flex items-center gap-2 content-compact text-default-secondary">
        <Spinner size="sm" tone="inherit" />
        {t_i18n("Running: {finished} of {total, plural, one {# run} other {# runs}} finished. The statistics come once every run has finished.", {
          finished,
          total: comparison.runs.length,
        })}
      </p>
    );
  }
  if (comparison.status !== "completed") {
    return (
      <p className="flex items-center gap-2 content-compact text-default-secondary">
        <AlertTriangle className="h-3.5 w-3.5 text-feedback-warning-primary" />
        {comparison.results
          ? t_i18n("A run of this comparison did not complete ({status}): its rows have no verdict.", { status: statusText(t_i18n, comparison.status) })
          : t_i18n("No results: this comparison ended ({status}).", { status: statusText(t_i18n, comparison.status) })}
      </p>
    );
  }
  return null;
}

/**
 * A row warning as a chip that stays inside its cell (UX r2 B1 on #10914): a label too
 * long for the Warnings column is cut with an ellipsis, and the tooltip names it whole,
 * then says what it changes in how the row reads when that is known.
 */
function WarningChip({ code, label }: { code: string; label: string }) {
  const { t_i18n } = useFormatter();
  const effect = placeboWarningEffect(code);
  return (
    <InfoLabel
      className="inline-flex min-w-0 max-w-full"
      content={
        <>
          {label}
          {effect && (
            <>
              <br />
              {t_i18n(effect)}
            </>
          )}
        </>
      }
    >
      <span
        className={cn(
          "inline-flex min-w-0 max-w-full items-center rounded-sm border px-1 content-caption",
          code === "few_entries"
            ? "border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.06] text-feedback-warning-tertiary"
            : "border-elevation-subtle-soft bg-elevation-hover text-default-secondary",
        )}
      >
        <span className="min-w-0 truncate">{label}</span>
      </span>
    </InfoLabel>
  );
}

/**
 * The comparison's rows as data table columns (standard v2: two lines per row at most, so the
 * row warnings and the skill share have their own columns), sortable on their figure.
 *
 * Fixed widths (UX round 1 on #10914): every cell holds on one line per row line, a long
 * target or note is cut with its tooltip, so each row keeps the 56 px height; the sized
 * columns plus Trials and Skill opened fit a 1432 px box (1728 screen) with no sideways
 * scroll. "Against no target" starts hidden, Trials and Skill opened are drawn from 1280 px:
 * all three come back from the Columns pill.
 */
function comparisonColumns(
  t_i18n: (id: string, values?: Record<string, string | number>) => string,
  targetLabel: (target: PlaceboTarget) => string,
  warningText: (code: string) => string,
  alpha: number,
  withSkillShare: boolean,
): DataTableColumn<PlaceboComparisonRow>[] {
  const verdictOf = (row: PlaceboComparisonRow): PlaceboVerdict => (row.verdict in PLACEBO_VERDICT_LABELS ? row.verdict : "not_computable");
  const pair = (label: string, stats: PlaceboPairStatistics | null) =>
    stats && (
      <span>
        {label}: D {stats.quality ? formatDifference(stats.quality) : "-"}
        {stats.tokens ? `, R ${formatRatio(stats.tokens)}` : ""}
      </span>
    );
  return [
    {
      key: "agent",
      header: "Agent / Model",
      sortValue: (row) => `${row.agent_name} ${row.model_id}`,
      csv: (row) => `${row.agent_name} / ${row.model_id}`,
      width: 220,
      cell: (row) => (
        <>
          <TruncatedText text={row.agent_name} className="content-compact font-medium" />
          <TruncatedText text={row.model_id} className="content-caption text-default-secondary" />
        </>
      ),
    },
    {
      key: "target",
      header: "Target",
      hint: t_i18n("What the placebo replaces: the persona, the prompts, the skills or one skill."),
      sortValue: (row) => targetLabel(row.target),
      csv: (row) => targetLabel(row.target),
      hideable: true,
      width: 160,
      cell: (row) => <TruncatedText text={targetLabel(row.target)} className="content-compact" />,
    },
    {
      key: "verdict",
      header: "Verdict",
      hint: t_i18n("The real agent against its placebo, on quality and tokens: an outcome improves or worsens when its Holm-adjusted p-value is below {alpha}.", { alpha }),
      sortValue: (row) => t_i18n(PLACEBO_VERDICT_LABELS[verdictOf(row)]),
      csv: (row) => t_i18n(PLACEBO_VERDICT_LABELS[verdictOf(row)]),
      width: 200,
      cell: (row) => {
        const verdict = verdictOf(row);
        const quality = row.vs_placebo.quality;
        const tokensAlone = quality ? t_i18n("On tokens alone; quality lower bound {low}", { low: quality.ci_low.toFixed(1) }) : "";
        return (
          <>
            <InfoLabel className="inline-flex" content={t_i18n(PLACEBO_VERDICT_RULES[verdict])}>
              <Badge variant={PLACEBO_VERDICT_BADGES[verdict]} className="whitespace-nowrap">
                {t_i18n(PLACEBO_VERDICT_LABELS[verdict])}
              </Badge>
            </InfoLabel>
            {row.rests_on_tokens_alone && quality && (
              <p className="truncate content-caption text-default-secondary">
                {/* The line may be cut: its tooltip names it whole first (UX r2 B2 on #10914). */}
                <InfoLabel
                  content={
                    <>
                      {tokensAlone}
                      <br />
                      {t_i18n(
                        "The verdict rests on tokens alone: quality did not differ from the placebo beyond chance. The value is the lower end of D's 95 % interval.",
                      )}
                    </>
                  }
                >
                  {tokensAlone}
                </InfoLabel>
              </p>
            )}
          </>
        );
      },
    },
    {
      key: "warnings",
      header: "Warnings",
      hint: t_i18n("What may weaken the row's verdict; each says, on focus, what it changes."),
      sortValue: (row) => row.warnings.length,
      csv: (row) => row.warnings.map(warningText).join("; "),
      hideable: true,
      width: 190,
      cell: (row) =>
        row.warnings.length === 0 ? (
          "-"
        ) : (
          // Two lines at most: the first warning, then the second with the count of the rest.
          // Each chip is cut inside the cell, the "+N" of the rest never (UX r2 B1 on #10914).
          <div className="flex min-w-0 max-w-full flex-col items-start gap-0.5">
            <WarningChip code={row.warnings[0]} label={warningText(row.warnings[0])} />
            {row.warnings.length > 1 && (
              <span className="flex min-w-0 max-w-full items-center gap-1 content-caption text-default-secondary">
                <WarningChip code={row.warnings[1]} label={warningText(row.warnings[1])} />
                {row.warnings.length > 2 && (
                  <InfoLabel className="shrink-0" content={row.warnings.slice(2).map(warningText).join("; ")}>
                    {`+${row.warnings.length - 2}`}
                  </InfoLabel>
                )}
              </span>
            )}
          </div>
        ),
    },
    {
      key: "quality",
      header: "Quality D [95 % CI]",
      hint: t_i18n("The real agent's Overall Quality minus its placebo's, in points, with its 95 % interval. Above 0, the target improves the answers."),
      sortValue: (row) => row.vs_placebo.quality?.estimate,
      csv: (row) => row.vs_placebo.quality?.estimate,
      width: 150,
      className: "whitespace-nowrap font-mono-plex content-compact",
      cell: (row) => {
        const quality = row.vs_placebo.quality;
        return quality ? (
          <InfoLabel content={t_i18n("p = {p}, Holm-adjusted {adjusted}", { p: quality.p, adjusted: quality.p_adjusted ?? "-" })}>
            {formatDifference(quality)}
          </InfoLabel>
        ) : (
          "-"
        );
      },
    },
    {
      key: "tokens",
      header: "Tokens R [95 % CI]",
      hint: t_i18n("The real agent's mean tokens divided by its placebo's, with its 95 % interval. Below 1, the real agent is cheaper."),
      sortValue: (row) => row.vs_placebo.tokens?.estimate,
      csv: (row) => row.vs_placebo.tokens?.estimate,
      width: 140,
      className: "whitespace-nowrap font-mono-plex content-compact",
      cell: (row) => {
        const tokens = row.vs_placebo.tokens;
        return tokens ? (
          <InfoLabel content={t_i18n("p = {p}, Holm-adjusted {adjusted}", { p: tokens.p, adjusted: tokens.p_adjusted ?? "-" })}>
            {formatRatio(tokens)}
          </InfoLabel>
        ) : (
          "-"
        );
      },
    },
    {
      key: "no_target",
      header: "Against no target",
      hint: t_i18n("The same D and R for the real agent and for its placebo, each against the arm with the target removed."),
      className: "content-caption text-default-secondary",
      hideable: true,
      defaultHidden: true,
      cell: (row) =>
        row.vs_no_target || row.placebo_vs_no_target ? (
          <div className="flex min-w-0 flex-col gap-0.5 font-mono-plex [&>span]:truncate">
            {pair(t_i18n("Real"), row.vs_no_target)}
            {pair(t_i18n("Placebo"), row.placebo_vs_no_target)}
          </div>
        ) : (
          "-"
        ),
    },
    {
      key: "trials",
      header: "Trials",
      hint: t_i18n("The entries counted, then the trials each arm ran in total over those entries."),
      sortValue: (row) => row.entries,
      csv: (row) => row.entries,
      className: "content-compact text-default-secondary",
      hideable: true,
      hideBelow: "7xl",
      cell: (row) => (
        <>
          <p className="truncate">{t_i18n("{count, plural, one {# entry} other {# entries}}", { count: row.entries })}</p>
          <p className="truncate content-caption">
            {t_i18n("real {real} · placebo {placebo}", { real: row.arms.real.trials, placebo: row.arms.placebo.trials })}
            {row.arms.no_target ? ` · ${t_i18n("no target {count}", { count: row.arms.no_target.trials })}` : ""}
          </p>
        </>
      ),
    },
    ...(withSkillShare
      ? [
          {
            key: "skill_opened",
            header: "Skill opened",
            hint: t_i18n(
              "The share of trials in which the agent opened the target skill (real arm) or the placebo skill that replaces it (placebo arm).",
            ),
            sortValue: (row: PlaceboComparisonRow) => row.arms.real.skill_loaded ?? undefined,
            csv: (row: PlaceboComparisonRow) => row.arms.real.skill_loaded ?? undefined,
            className: "truncate content-caption text-default-secondary",
            hideable: true,
            hideBelow: "7xl" as const,
            cell: (row: PlaceboComparisonRow) =>
              row.arms.real.skill_loaded === null
                ? "-"
                : t_i18n("real {real} · placebo {placebo}", {
                    real: formatShare(row.arms.real.skill_loaded),
                    placebo: formatShare(row.arms.placebo.skill_loaded),
                  }),
          },
        ]
      : []),
  ];
}
