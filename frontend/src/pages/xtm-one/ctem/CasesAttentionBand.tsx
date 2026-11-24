// SPDX-License-Identifier: Apache-2.0

/**
 * "Act on this first" above the CTEM Cases list (#1176): the open cases with
 * the highest attention score (ctem/case-attention.ts), each with the one-line
 * why and the next action. The band reads the same rows as the table; it is
 * absent when no case is open.
 *
 * The next action is the control it names (UX round 1 on #1182, B3): "Assign owner"
 * and "Set due date" open the Cases bulk dialog for that one case, with the same
 * labels; "Open the case" links to it.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useAuth } from "@/hooks/useAuth";
import { TruncatedText } from "@/components/ui/truncated-text";
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { CasesBulkActions } from "./CasesBulkActions";
import { CasePriorityChip } from "./cases-shared";
import { CtemWidgetHeader } from "./CtemWidgetHeader";
import { CaseAttentionWeightsDialog } from "./CaseAttentionWeightsDialog";
import { rankByAttention, type CaseAttention, type CaseAttentionReason, type CaseAttentionWeights, type CaseNextAction } from "./case-attention";

/** How many cases the band shows. */
const ATTENTION_BAND_SIZE = 3;

/** The bulk dialog a next action opens; "open_case" is a link. */
export const NEXT_ACTION_DIALOG: Partial<Record<CaseNextAction, "assign" | "due">> = {
  assign: "assign",
  set_due: "due",
};

/**
 * The one-line why of an attention score, in the reader's language: the list band
 * and the case page say it with the same words (#1176).
 */
export function useAttentionWhy(): (attention: CaseAttention) => string {
  const { t_i18n } = useFormatter();
  const reasonText = (r: CaseAttentionReason): string | null => {
    switch (r.kind) {
      case "kev":
        // No asset known: the KEV count alone, never "on 0 assets" (UX round 1 on #1182, B2).
        return r.assets > 0
          ? t_i18n("{count, plural, one {# KEV} other {# KEV}} on {assets, plural, one {# asset} other {# assets}}", { count: r.count, assets: r.assets })
          : t_i18n("{count, plural, one {# KEV} other {# KEV}}", { count: r.count });
      case "crown_jewel":
        return t_i18n("{count, plural, one {# primary asset touched} other {# primary assets touched}}", { count: r.count });
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
  return (attention) => attention.reasons.map(reasonText).filter((s): s is string => Boolean(s)).join(", ");
}

export function CasesAttentionBand({ cases, now, weights }: { cases: CtemCaseSummary[]; now: number; weights: CaseAttentionWeights }) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const [editing, setEditing] = useState(false);
  const whyOf = useAttentionWhy();
  const top = rankByAttention(cases, now, weights).slice(0, ATTENTION_BAND_SIZE);
  if (top.length === 0) return null;

  return (
    <Card data-testid="ctem-cases-attention">
      <CardContent className="space-y-3 p-4">
        <CtemWidgetHeader
          level={2}
          title={t_i18n("Act on this first")}
          hint={t_i18n("Open cases ranked by priority, KEV, primary assets touched, due date, owner and age. The line under each case says why it ranks there.")}
          hintLabel={t_i18n("How cases are ranked")}
          actions={
            isAdmin ? (
              <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(true)} data-testid="ctem-cases-attention-weights-open">
                <Icon name="sliders-horizontal" size={16} />
                <span>{t_i18n("Ranking points")}</span>
              </Button>
            ) : undefined
          }
        />
        {isAdmin && <CaseAttentionWeightsDialog open={editing} onClose={() => setEditing(false)} />}
        <ol className="flex flex-col divide-y divide-[var(--border-elevation-subtle-soft)]">
          {top.map(({ kase, attention }) => {
            const why = whyOf(attention);
            return (
              <li key={kase.id} className="flex items-center gap-3 py-2" data-testid="ctem-cases-attention-row">
                <CasePriorityChip priority={kase.priority} />
                <div className="min-w-0 flex-1">
                  {/* A cut title or why line is named whole by a tooltip (B4). */}
                  <Link to={`/ctem/cases/${kase.id}`} className="block min-w-0 font-medium hover:underline">
                    <TruncatedText text={kase.title} />
                  </Link>
                  <div className="content-caption text-default-secondary" data-testid="ctem-cases-attention-why">
                    <TruncatedText text={why} />
                  </div>
                </div>
                <div className="shrink-0" data-testid="ctem-cases-attention-next">
                  {NEXT_ACTION_DIALOG[attention.next] ? (
                    <CasesBulkActions cases={[kase]} only={NEXT_ACTION_DIALOG[attention.next]} onDone={() => undefined} />
                  ) : (
                    <Link to={`/ctem/cases/${kase.id}`} className="whitespace-nowrap content-compact text-accent hover:underline">
                      {t_i18n("Open the case")}
                    </Link>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      </CardContent>
    </Card>
  );
}
