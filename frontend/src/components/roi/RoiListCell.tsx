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
 * The "Net value (30d)" cell of the agent and fleet lists (#9836 R6b, design
 * dev-docs/roi-outcomes.md 10.8): the shared `RoiFigure` (layout `inline`, no
 * range in the cell, the "How this is computed" button beside it), fed by the
 * page's one `GET /roi/summary` read: the ledger's figures of the object, the
 * agent page hero's (#12077 L7a, invariant d). Nothing declared: "-" with the reason,
 * and for a reader who may edit that agent a "Declare a unit" link to its
 * Outcomes picker; the cell is `interactive`, so a click never opens the row. Fewer than k
 * people behind the agent or fleet (#12077 L7f, design 12.10 row "List column cell"): "-"
 * whose tip gives the reader's own use, or says the totals are hidden; never a figure that
 * reads as the total, and the row sorts after every figure (`summarySortValue`).
 */
import { Link } from "react-router-dom";
import { RoiFigure } from "@/components/roi/RoiFigure";
import { InfoTip } from "@/components/ui/info-tip";
import type { RoiSummaryItem } from "@/hooks/api/useRoiReportApi";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { agentPickUnitPath } from "@/lib/roi-outcomes";
import { roiColumnMetric, summaryFigures, summaryUnderK } from "@/lib/roi-summary";

/** The tip of an under-k cell (design 12.10, under-k table). @i18n-keys */
const UNDER_K_TIP = {
  label: "Why the totals are hidden",
  own: "{k, plural, one {Your use: {figure}; totals shown from {shown} person} other {Your use: {figure}; totals shown from {shown} people}}",
  hidden: "{k, plural, one {Fewer than {shown} person: hidden} other {Fewer than {shown} people: hidden}}",
} as const;

interface RoiListCellProps {
  item: RoiSummaryItem | undefined;
  currency: string;
  costMasked: boolean;
  /** The agent's id when the reader may edit it: the empty cell offers "Declare a unit". */
  declareAgentId?: string;
  /** The page's read is still running. */
  loading?: boolean;
}

export function RoiListCell({ item, currency, costMasked, declareAgentId, loading }: RoiListCellProps) {
  const { t_i18n } = useFormatter();
  const nf = useNumberFormat(currency);
  if (loading) return <span className="text-default-secondary" aria-busy="true">-</span>;
  const underK = summaryUnderK(item);
  if (underK && item?.figures) {
    // The reader's own share, never drawn as the agent's or fleet's figure (design 12.10).
    const totals = item.figures.totals;
    const own = costMasked ? totals.value : (totals.net_value ?? totals.value);
    const values = { k: underK.k, shown: nf.count(underK.k), figure: nf.money(own) };
    return (
      <span className="inline-flex items-center gap-1 whitespace-nowrap text-default-secondary" data-testid="roi-list-cell-under-k">
        <span>-</span>
        <InfoTip label={t_i18n(UNDER_K_TIP.label)} content={t_i18n(underK.viewerUsed ? UNDER_K_TIP.own : UNDER_K_TIP.hidden, values)} />
      </span>
    );
  }
  const figures = summaryFigures(item);
  if (!figures) {
    return (
      <span className="inline-flex items-center gap-1 whitespace-nowrap text-default-secondary" data-testid="roi-list-cell-empty">
        <span>-</span>
        <InfoTip label={t_i18n("Why there is no figure")} content={t_i18n("No unit of work declared")} />
        {declareAgentId && (
          <Link
            to={agentPickUnitPath(declareAgentId)}
            className="rounded-sm text-accent hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
          >
            {t_i18n("Declare a unit")}
          </Link>
        )}
      </span>
    );
  }
  return (
    <RoiFigure
      figures={figures}
      metric={roiColumnMetric(costMasked)}
      currency={currency}
      window="30d"
      costMasked={costMasked}
      layout="inline"
      hideRange
    />
  );
}
