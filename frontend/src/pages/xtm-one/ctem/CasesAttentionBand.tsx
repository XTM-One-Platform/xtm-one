// SPDX-License-Identifier: Apache-2.0

/**
 * "Act on this first" above the CTEM Cases list (#1176): the open cases with
 * the highest attention score (ctem/case-attention.ts), each with the one-line
 * why and the next action. The band reads the same rows as the table; it is
 * absent when no case is open.
 */
import { Link } from "react-router-dom";
import { Card, CardContent } from "@/components/ui/card";
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { CasePriorityChip } from "./cases-shared";
import { CtemWidgetHeader } from "./CtemWidgetHeader";
import { rankByAttention, type CaseAttentionReason, type CaseNextAction } from "./case-attention";

/** How many cases the band shows. */
export const ATTENTION_BAND_SIZE = 3;

const NEXT_ACTION_LABEL: Record<CaseNextAction, string> = {
  assign: "Assign an owner",
  set_due: "Set a due date",
  open_case: "Open the case",
};

export function CasesAttentionBand({ cases, now }: { cases: CtemCaseSummary[]; now: number }) {
  const { t_i18n } = useFormatter();
  const top = rankByAttention(cases, now).slice(0, ATTENTION_BAND_SIZE);
  if (top.length === 0) return null;

  const reasonText = (r: CaseAttentionReason): string | null => {
    switch (r.kind) {
      case "kev":
        return t_i18n("{count, plural, one {# KEV} other {# KEV}} on {assets, plural, one {# asset} other {# assets}}", { count: r.count, assets: r.assets });
      case "crown_jewel":
        return t_i18n("{count, plural, one {# crown jewel touched} other {# crown jewels touched}}", { count: r.count });
      case "overdue":
        return t_i18n("Overdue");
      case "due_soon":
        return t_i18n("{days, plural, =0 {Due today} one {Due in # day} other {Due in # days}}", { days: r.days });
      case "no_owner":
        return t_i18n("No owner");
      case "age":
        return t_i18n("{days, plural, =0 {Opened today} one {Opened # day ago} other {Opened # days ago}}", { days: r.days });
      default:
        return null; // the priority is the chip
    }
  };

  return (
    <Card data-testid="ctem-cases-attention">
      <CardContent className="space-y-3 p-4">
        <CtemWidgetHeader
          level={2}
          title={t_i18n("Act on this first")}
          hint={t_i18n("Open cases ranked by priority, KEV, crown jewels touched, due date, owner and age. The line under each case says why it ranks there.")}
          hintLabel={t_i18n("How cases are ranked")}
        />
        <ol className="flex flex-col divide-y divide-[var(--border-elevation-subtle-soft)]">
          {top.map(({ kase, attention }) => {
            const why = attention.reasons.map(reasonText).filter((s): s is string => Boolean(s)).join(", ");
            return (
              <li key={kase.id} className="flex items-center gap-3 py-2" data-testid="ctem-cases-attention-row">
                <CasePriorityChip priority={kase.priority} />
                <div className="min-w-0 flex-1">
                  <Link to={`/ctem/cases/${kase.id}`} className="block truncate font-medium hover:underline">
                    {kase.title}
                  </Link>
                  <p className="truncate content-caption text-default-secondary" data-testid="ctem-cases-attention-why">
                    {why}
                  </p>
                </div>
                <Link
                  to={`/ctem/cases/${kase.id}`}
                  className="whitespace-nowrap content-compact text-accent hover:underline"
                  data-testid="ctem-cases-attention-next"
                >
                  {t_i18n(NEXT_ACTION_LABEL[attention.next])}
                </Link>
              </li>
            );
          })}
        </ol>
      </CardContent>
    </Card>
  );
}
