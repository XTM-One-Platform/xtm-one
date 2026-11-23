// SPDX-License-Identifier: Apache-2.0

/**
 * The top band of a case page (#1176, slice 3): when the case was opened, how
 * old it is and when it last changed, and, for an open case, why it needs
 * attention now with the same explained score as the "Act on this first" band
 * of the Cases list (ctem/case-attention.ts) and its next action. A resolved
 * case or an accepted risk shows the dates alone: nothing to act on.
 */
import type { ReactNode } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { TruncatedText } from "@/components/ui/truncated-text";
import { isOpenCaseState, type CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { CasesBulkActions } from "./CasesBulkActions";
import { NEXT_ACTION_DIALOG, useAttentionWhy } from "./CasesAttentionBand";
import { caseAttention, caseOpenedAt } from "./case-attention";
import { CtemWidgetHeader } from "./CtemWidgetHeader";
import { useCaseAttentionWeights } from "./useCaseAttentionWeights";

const DAY_MS = 24 * 3600 * 1000;

function Fact({ label, children, testId }: { label: string; children: ReactNode; testId: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5" data-testid={testId}>
      <span className="content-caption text-default-secondary">{label}</span>
      <span className="content-base">{children}</span>
    </div>
  );
}

export function CaseAttentionNow({ c, now, onChanged }: { c: CtemCaseSummary; now: number; onChanged: () => void }) {
  const { t_i18n } = useFormatter();
  const whyOf = useAttentionWhy();
  const attention = caseAttention(c, now, useCaseAttentionWeights());
  const open = isOpenCaseState(c.state);
  const opened = caseOpenedAt(c);
  const days = opened ? Math.max(0, Math.floor((now - Date.parse(opened)) / DAY_MS)) : null;
  const why = open ? whyOf(attention) : "";
  const dialog = NEXT_ACTION_DIALOG[attention.next];

  return (
    <Card data-testid="ctem-case-attention-now">
      <CardContent className="flex flex-col gap-3 p-4">
        {open && why && (
          <div className="flex min-w-0 flex-col gap-1">
            {/* The shared section title of the CTEM pages, h3 in the case page outline (UX r1 B1). */}
            <CtemWidgetHeader
              level={3}
              title={t_i18n("Why this case needs attention now")}
              testId="ctem-case-attention-title"
              actions={
                dialog ? (
                  <div data-testid="ctem-case-attention-next">
                    <CasesBulkActions cases={[c]} only={dialog} onDone={onChanged} />
                  </div>
                ) : undefined
              }
            />
            <div className="content-compact text-default-secondary" data-testid="ctem-case-attention-why">
              <TruncatedText text={why} />
            </div>
          </div>
        )}
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          {/* One word for this date, the why line's "Opened" (UX r1 B2). */}
          <Fact label={t_i18n("Opened")} testId="ctem-case-created">
            <DateTimeCell value={opened} />
          </Fact>
          <Fact label={t_i18n("Age")} testId="ctem-case-age">
            {days === null ? "-" : t_i18n("{days, plural, =0 {Today} one {# day} other {# days}}", { days })}
          </Fact>
          <Fact label={t_i18n("Last change")} testId="ctem-case-last-change">
            <DateTimeCell value={c.updated_at ?? c.state_since} />
          </Fact>
          <Fact label={t_i18n("Due date")} testId="ctem-case-due">
            <DateTimeCell value={c.sla_due_at ?? c.due_at} />
          </Fact>
        </div>
      </CardContent>
    </Card>
  );
}
