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
 * The shell of an evaluation's pages (#12282, #2): a crumb trail ("Evaluations ›
 * {evaluation} › Leaderboard", or "Evaluations › Compare 2 evaluations"), the title and
 * its subtitle, and one tab row joining the four pages: Compare evaluations, Leaderboard,
 * Run review and Results. Every tab is a link with its own route and query, so the
 * comparison page and the run review render the same row.
 */

import { Link } from "react-router-dom";
import type { ReactNode } from "react";

import { DetailTabs, type DetailTab } from "@/components/ui/detail-tabs";
import { useFormatter } from "@/i18n";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { cn } from "@/lib/utils";

/** The pages of an evaluation; `pairs` compares evaluations (one evaluation: run against run). */
export type EvaluationPage = "pairs" | "leaderboard" | "review" | "results";

/** The tab labels, read by `t_i18n` (in `DetailTabs`). @i18n-keys */
export const EVALUATION_PAGE_LABELS = {
  compare: "Compare evaluations",
  pairs: "Run against run",
  leaderboard: "Leaderboard",
  review: "Run review",
  results: "Results",
} as const;

export interface Crumb {
  label: string;
  to?: string;
}

/**
 * The tab row of an evaluation's pages. `ids`: the evaluations compared, the page's own
 * first (one: that evaluation alone); `runs`: the runs the comparison picked, kept on its
 * tabs; `reviewRun`: the run the Run review tab opens (none: no tab).
 */
export function evaluationPageTabs({
  ids,
  runs = [],
  reviewRun,
}: {
  ids: readonly string[];
  runs?: readonly string[];
  reviewRun: string | null;
}): DetailTab<EvaluationPage>[] {
  const query = (withRuns: boolean) => {
    const p = new URLSearchParams();
    p.set("ids", ids.join(","));
    if (withRuns && runs.length) p.set("runs", runs.join(","));
    return `?${p.toString().replace(/%2C/g, ",")}`;
  };
  const single = ids.length === 1;
  const board: DetailTab<EvaluationPage> = {
    key: "leaderboard",
    label: EVALUATION_PAGE_LABELS.leaderboard,
    to: `/evaluations/compare${single ? "" : "/leaderboard"}${query(false)}`,
  };
  const review: DetailTab<EvaluationPage>[] = reviewRun
    ? [{ key: "review", label: EVALUATION_PAGE_LABELS.review, to: `/evaluations/review?run=${encodeURIComponent(reviewRun)}` }]
    : [];
  const results: DetailTab<EvaluationPage> = { key: "results", label: EVALUATION_PAGE_LABELS.results, to: `/evaluations/compare/results${query(false)}` };
  if (single) {
    return [board, ...review, results, { key: "pairs", label: EVALUATION_PAGE_LABELS.pairs, to: `/evaluations/compare/pairs${query(true)}` }];
  }
  return [{ key: "pairs", label: EVALUATION_PAGE_LABELS.compare, to: `/evaluations/compare${query(true)}` }, board, ...review, results];
}

/** The crumb trail, the title, the subtitle and the page tabs. */
export function EvaluationPageShell({
  crumbs,
  title,
  subtitle,
  tabs,
  active,
  actions,
}: {
  crumbs: Crumb[];
  title: string;
  subtitle?: string | null;
  tabs: DetailTab<EvaluationPage>[];
  active: EvaluationPage;
  /** Buttons on the title's line. */
  actions?: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  return (
    <header className="space-y-3" data-testid="evaluation-page-shell">
      <div className="space-y-1">
        <nav aria-label={t_i18n("Breadcrumb")}>
          <ol className="flex flex-wrap items-center gap-1.5 content-compact text-default-secondary">
            {crumbs.map((crumb, i) => (
              <li key={`${i}-${crumb.label}`} className="inline-flex min-w-0 items-center gap-1.5">
                {i > 0 && <span aria-hidden>/</span>}
                {crumb.to ? (
                  <Link to={crumb.to} className={cn("truncate rounded-sm transition-colors hover:text-accent", BUTTON_ROLE_FOCUS_RING)}>
                    {crumb.label}
                  </Link>
                ) : (
                  <span className="truncate text-default-primary" aria-current="page">
                    {crumb.label}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </nav>
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0 space-y-0.5">
            <h1 className="break-words text-2xl font-bold">{title}</h1>
            {subtitle && <p className="content-compact text-default-secondary">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </div>
      </div>
      {tabs.length > 1 && <DetailTabs tabs={tabs} active={active} ariaLabel={t_i18n("Page")} />}
    </header>
  );
}
