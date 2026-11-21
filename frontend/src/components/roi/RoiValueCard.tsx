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
 * The home dashboard's "Value" card (#9836 R6b, design dev-docs/roi-outcomes.md
 * 10.8): "Hours saved" and "Net value" over 30 days for the reader's scope
 * (the whole organization for an administrator, his own agents and fleets
 * otherwise, the Value page's opening scope), each through the shared
 * `RoiFigure` with its "How this is computed" button, and "See the value" to
 * `/tasks/value`. Costs masked: "Hours saved" and "Value". It reads the Value page's
 * own report and draws its ledger hero (`figures`, #12077 L7a, design 12.8:
 * `figures(mine)` or the organization's), the Value page hero's figures
 * (one cache entry, so the card and the page never differ), starts
 * no run and calls no model. Community Edition: the card says where Value
 * lives, never a dead end (b3cd836b).
 */
import { valuePath } from "@/lib/task-routes";
import { Link } from "react-router-dom";
import { ArrowUpRight, TrendingUp } from "lucide-react";
import { RoiFigure } from "@/components/roi/RoiFigure";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { useValueReport } from "@/hooks/api/useRoiReportApi";
import { useEnterpriseLockedReason } from "@/hooks/useEnterpriseUpsell";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { VALUE_SCOPE_LINES, ledgerHeroFigures } from "@/lib/roi-value";
import { roiColumnMetric } from "@/lib/roi-summary";

export function RoiValueCard() {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const locked = useEnterpriseLockedReason();
  const scope = isAdmin ? "organization" : "mine";
  const report = useValueReport(scope === "organization" ? "platform" : "mine", "30d", { enabled: !locked });
  const costMasked = Boolean(report.data?.cost_masked);
  // The ledger's hero, as the Value page draws it; the report's own figures from an older backend only.
  const figures = report.data ? (ledgerHeroFigures(report.data) ?? report.data.organization) : null;

  return (
    <Card className="flex h-full flex-col" data-testid="home-value-card">
      <CardContent className="flex h-full flex-col gap-3 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 content-base font-semibold">
              <TrendingUp className="h-4 w-4 text-accent" aria-hidden /> {t_i18n("Value")}
            </h2>
            <p className="content-compact text-default-secondary" data-testid="home-value-scope">
              {t_i18n(VALUE_SCOPE_LINES[scope])} · {t_i18n("Last 30 days")}
            </p>
          </div>
          {/* The same header action as the sibling widgets (Agents "All", Last Conversations, Live Activity): 28 px, ArrowUpRight (UX r1 B1). */}
          <Button asChild variant="ghost" size="sm" className="h-7 shrink-0 content-compact gap-1" data-testid="home-value-see">
            <Link to={valuePath()}>
              <ArrowUpRight className="h-3 w-3" aria-hidden /> {t_i18n("See the value")}
            </Link>
          </Button>
        </div>
        {locked ? (
          <p className="content-compact text-default-secondary">{t_i18n("Value is part of Enterprise Edition.")}</p>
        ) : report.isError ? (
          <div role="alert" className="flex flex-wrap items-center gap-3 content-compact text-feedback-error-primary">
            {t_i18n("The value could not be read.")}
            <Button type="button" variant="outline" size="sm" onClick={() => report.refetch()}>
              {t_i18n("Retry")}
            </Button>
          </div>
        ) : !report.data || !figures ? (
          <div className="grid flex-1 auto-rows-fr grid-cols-2 gap-3" aria-busy="true">
            <div className="rounded-sm bg-elevation-hover" />
            <div className="rounded-sm bg-elevation-hover" />
          </div>
        ) : (
          // Two figures, one height (auto-rows-fr), the same component and strings as the Value page.
          <div className="grid flex-1 auto-rows-fr grid-cols-2 gap-3">
            <RoiFigure figures={figures} metric="hours" currency={report.data.currency} window="30d" costMasked={costMasked} layout="card" />
            <RoiFigure
              figures={figures}
              metric={roiColumnMetric(costMasked)}
              currency={report.data.currency}
              window="30d"
              costMasked={costMasked}
              layout="card"
            />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
