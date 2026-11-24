// SPDX-License-Identifier: Apache-2.0

/**
 * "How the Exposure Posture is computed" (#11689): the dialog the command
 * center hero opens, now with both readings.
 *
 * One row per CTEM stage: its weight for the vertical, its measured score
 * (quantitative, `ctem_scoring.score_stage`) and its score in your context
 * (qualitative, `ctem_context_scoring.qualitative_stage_score`), each in its
 * band colour. Two totals: the measured posture (`compute_posture`) and the
 * posture in your context (`with_context_scores`), each the weighted average
 * of its column renormalised over the stages that have a score in it. Every
 * figure is the payload's own: nothing is recomputed here.
 */
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useFormatter } from "@/i18n";
import { CTEM_BAND_CONFIG, CTEM_STAGE_CONFIG, CTEM_STAGE_KEYS } from "@/lib/constants";
import { cn } from "@/lib/utils";
import type { CtemBand, CtemPosture } from "@/types";

function bandColor(score: number | null | undefined, band: CtemBand | undefined) {
  return score != null ? (CTEM_BAND_CONFIG[band ?? "unknown"] ?? CTEM_BAND_CONFIG.unknown).color : "text-default-secondary";
}

export function PostureExplainDialog({
  open,
  onOpenChange,
  posture,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  posture: CtemPosture | null | undefined;
}) {
  const { t_i18n } = useFormatter();
  const byKey = new Map((posture?.stages ?? []).map((s) => [s.key, s]));
  const contextMissing = posture?.context?.state === "missing" || posture?.qualitative_score == null;
  const num = "w-20 pl-4 text-right tabular-nums";
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{t_i18n("How the Exposure Posture is computed")}</DialogTitle>
          <DialogDescription>
            {t_i18n("Two weighted averages of your five CTEM stage scores, one per reading, each renormalized over the stages that have a score.")}
          </DialogDescription>
        </DialogHeader>
        {/* The table sits on the elevation layer: the band colours read 4.5:1 there,
            not on the dialog's layer-2 fill (At risk 4.40:1 light, measured). */}
        <div className="rounded-sm layer-1 bg-elevation-default px-3 py-2" data-testid="ctem-posture-explain-readings">
        <table className="w-full border-collapse content-compact" data-testid="ctem-posture-explain-table">
          <thead>
            <tr className="content-caption uppercase tracking-wider text-default-secondary">
              <th scope="col" className="pb-2 text-left font-normal">{t_i18n("Stage")}</th>
              <th scope="col" className={cn(num, "pb-2 font-normal")}>{t_i18n("Weight")}</th>
              <th scope="col" className={cn(num, "pb-2 font-normal")}>{t_i18n("Measured")}</th>
              <th scope="col" className={cn("w-36 pl-4 text-right pb-2 font-normal")}>{t_i18n("In your context")}</th>
            </tr>
          </thead>
          <tbody>
            {CTEM_STAGE_KEYS.map((key) => {
              const st = byKey.get(key);
              const weight = posture?.weights?.[key];
              const measured = st?.score ?? null;
              const context = measured != null ? (st?.qualitative_score ?? null) : null;
              return (
                <tr key={key} data-testid={`ctem-posture-explain-${key}`}>
                  <th scope="row" className="py-1 text-left font-normal truncate">{t_i18n(CTEM_STAGE_CONFIG[key]?.label ?? st?.label ?? key)}</th>
                  <td className={cn(num, "py-1 content-caption text-default-secondary")}>{weight != null ? `${Math.round(weight * 100)}%` : "--"}</td>
                  <td className={cn(num, "py-1 font-semibold", bandColor(measured, st?.band))}>{measured ?? "--"}</td>
                  <td className={cn("w-36 pl-4 text-right tabular-nums py-1 font-semibold", bandColor(context, st?.qualitative_band))}>{context ?? "--"}</td>
                </tr>
              );
            })}
          </tbody>
          <tfoot>
            <tr className="border-t border-elevation-subtle-soft" data-testid="ctem-posture-explain-total">
              <th scope="row" className="pt-3 text-left content-base font-semibold">{t_i18n("Exposure Posture")}</th>
              <td className={cn(num, "pt-3")} />
              <td className={cn(num, "pt-3 text-lg font-bold", bandColor(posture?.posture_score, posture?.posture_band))}>
                {posture?.posture_score ?? "--"}
              </td>
              <td className={cn("w-36 pl-4 text-right tabular-nums pt-3 text-lg font-bold", bandColor(posture?.qualitative_score, posture?.qualitative_band))}>
                {posture?.qualitative_score ?? "--"}
              </td>
            </tr>
          </tfoot>
        </table>
        </div>
        <div className="space-y-2 content-caption leading-relaxed text-default-secondary">
          <p>
            <span className="font-semibold text-default-primary">{t_i18n("Measured")}</span>{" "}
            {t_i18n("is quantitative: each stage scored from the connected platforms' data alone.")}
          </p>
          <p>
            <span className="font-semibold text-default-primary">{t_i18n("In your context")}</span>{" "}
            {t_i18n("is the qualitative adjustment: each measured stage score weighed by the share of the organization context it reads that you filled in (40% plus 60% of that share). Prioritization also averages in the known-exploited vulnerabilities of your software.")}
          </p>
          {contextMissing && <p data-testid="ctem-posture-explain-no-context">{t_i18n("Add your organization context in Settings to get the posture in your context.")}</p>}
          <p>
            {t_i18n("Weights are per vertical (shown above) and renormalized over the stages that have data, so a stage with no connected product never drags the score down. The score is")}{" "}
            {t_i18n("0-100, higher is better: under 40 Critical, 40-59 At risk, 60-74 Fair, 75-89 Good, 90+ Strong.")}
          </p>
        </div>
      </DialogContent>
    </Dialog>
  );
}
