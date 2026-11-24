// SPDX-License-Identifier: Apache-2.0

/**
 * The structured hover card of a CTEM score (#11689): one surface shared by
 * the CTEM widgets in place of a run of plain text.
 *
 * It reads, top to bottom: the title (the stage or the posture), a small
 * table with the two readings side by side (Measured, the quantitative score
 * from the connected platforms' data; In your context, the qualitative
 * adjustment for the organization profile), each with its score and band and
 * the 30-day change of the measured one; then the stage metric, said once
 * when it is the score's own input (Discovery's coverage is its score); then
 * one line on how the context reading is derived, or the link to add it.
 *
 * The definitions come from the code, never a paraphrase: the measured score
 * is `ctem_scoring.score_stage` (the connected modules' facts only) and the
 * in-context score is `ctem_context_scoring.qualitative_stage_score`, the
 * measured score times 0.4 + 0.6 x the share of the stage's context fields
 * filled (Prioritization also averages in the known-exploited CVEs of the
 * declared software inventory). It is content for `Tip`: no trigger, no state, no
 * request.
 */
import type { ReactNode } from "react";
import { Icon } from "@filigran/design-system";

import { useFormatter } from "@/i18n";
import { CTEM_BAND_CONFIG } from "@/lib/constants";
import { cn } from "@/lib/utils";
import type { CtemBand } from "@/types";

interface CtemScoreTipReading {
  score: number | null;
  band: CtemBand | undefined | null;
}

interface CtemScoreTipMetric {
  label: string;
  /** 0-100, a percentage. */
  value: number;
  /** Its 30-day change; null when fewer than two days are recorded. */
  delta: number | null;
  /** The metric is the score's own input: its change is the score's, said once. */
  sameAsScore?: boolean;
}

export interface CtemScoreTipProps {
  title: string;
  measured: CtemScoreTipReading;
  context: CtemScoreTipReading;
  /** The measured score's 30-day change; null when fewer than two days are recorded. */
  delta: number | null;
  metric?: CtemScoreTipMetric | null;
  /** Shown instead of the table when the stage has no measured score. */
  emptyText?: string;
  /** No organization context: the context cell says so. */
  noContext?: boolean;
  /** One extra line under the table (a posture target). */
  footer?: ReactNode;
}

function signed(n: number) {
  return n > 0 ? `+${n}` : `${n}`;
}

function bandConf(band: CtemBand | undefined | null) {
  return CTEM_BAND_CONFIG[band ?? "unknown"] ?? CTEM_BAND_CONFIG.unknown;
}

/** The score and band of one reading, in its band colour; "--" without a score. */
function ReadingCell({ reading, testId }: { reading: CtemScoreTipReading; testId: string }) {
  const { t_i18n } = useFormatter();
  const conf = bandConf(reading.band);
  if (reading.score == null) {
    return (
      <td className="py-1 pl-3 text-right tabular-nums text-default-secondary" data-testid={testId}>
        --
      </td>
    );
  }
  return (
    <td className="py-1 pl-3 text-right" data-testid={testId}>
      <span className={cn("font-semibold tabular-nums", conf.color)}>{reading.score}</span>{" "}
      <span className={cn("content-caption", conf.color)}>{t_i18n(conf.label)}</span>
    </td>
  );
}

export function CtemScoreTip({ title, measured, context, delta, metric, emptyText, noContext, footer }: CtemScoreTipProps) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex w-[17rem] max-w-full flex-col gap-2 py-1" data-testid="ctem-score-tip">
      <p className="content-compact font-semibold text-default-primary">{title}</p>
      {measured.score == null ? (
        <p className="content-caption text-default-secondary">{emptyText}</p>
      ) : (
        <>
          {/* The readings sit on their own surface: the band colours read 4.5:1 on the
              elevation layer, never on the tooltip fill (2.98:1 light, measured). */}
          <div className="rounded-sm layer-1 bg-elevation-default px-2 py-1" data-testid="ctem-score-tip-readings">
          <table className="w-full border-collapse content-caption" data-testid="ctem-score-tip-table">
            <thead>
              <tr className="text-default-secondary">
                <th scope="col" className="sr-only">{t_i18n("Reading")}</th>
                <th scope="col" className="pb-1 pl-3 text-right font-medium">
                  <span className="inline-flex items-center gap-1"><Icon name="database" size={16} aria-hidden /> {t_i18n("Measured")}</span>
                </th>
                <th scope="col" className="pb-1 pl-3 text-right font-medium">
                  <span className="inline-flex items-center gap-1"><Icon name="building-2" size={16} aria-hidden /> {t_i18n("In your context")}</span>
                </th>
              </tr>
            </thead>
            <tbody className="border-t border-elevation-subtle-soft">
              <tr>
                <th scope="row" className="py-1 text-left font-normal text-default-secondary">{t_i18n("Score")}</th>
                <ReadingCell reading={measured} testId="ctem-score-tip-measured" />
                {noContext || context.score == null ? (
                  <td className="py-1 pl-3 text-right text-default-secondary" data-testid="ctem-score-tip-context">
                    {noContext ? t_i18n("No context") : "--"}
                  </td>
                ) : (
                  <ReadingCell reading={context} testId="ctem-score-tip-context" />
                )}
              </tr>
              <tr>
                <th scope="row" className="py-1 text-left font-normal text-default-secondary">{t_i18n("30-day change")}</th>
                <td className="py-1 pl-3 text-right tabular-nums text-default-primary" data-testid="ctem-score-tip-delta">
                  {delta != null ? signed(delta) : "--"}
                </td>
                <td className="py-1 pl-3 text-right text-default-secondary" />
              </tr>
            </tbody>
          </table>
          </div>
          {metric && (
            <p className="flex items-baseline justify-between gap-3 border-t border-elevation-subtle-soft pt-2 content-caption" data-testid="ctem-score-tip-metric">
              <span className="text-default-secondary">
                {metric.sameAsScore ? t_i18n("{metric}, the input of the score", { metric: metric.label }) : metric.label}
              </span>
              <span className="tabular-nums text-default-primary">
                {Math.round(metric.value)}%
                {!metric.sameAsScore && metric.delta != null ? ` (${t_i18n("{delta} / 30 d", { delta: signed(metric.delta) })})` : ""}
              </span>
            </p>
          )}
        </>
      )}
      <p className="border-t border-elevation-subtle-soft pt-2 content-caption leading-relaxed text-default-secondary">
        {t_i18n("Measured is quantitative: the connected platforms' data alone. In your context is the qualitative adjustment: the measured score weighed by the share of the organization context this stage reads that you filled in (40% plus 60% of that share).")}
      </p>
      {footer}
    </div>
  );
}
