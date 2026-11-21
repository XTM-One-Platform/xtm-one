// SPDX-License-Identifier: Apache-2.0

/**
 * The loop trace (dev-docs/ctem-loop-visibility.md section 4): the only new
 * surface of the CTEM loop program. A centred dialog, opened by the `?loop=`
 * search parameter of the current page (`signal:<id>` or `case:<id>`), so a
 * trace can be shared and reopened after a reload; closing it removes the
 * parameter and focus returns to the row that opened it.
 *
 * It draws the ten stages the object went through, the current one lit, the
 * stage where the loop stopped with its reason, and the stages not reached
 * greyed. It reads through the same API as the rows, so what the viewer
 * cannot read answers "This item is not available to you". The activity
 * stream refreshes the signal query, so a stage lights without a reload.
 */
import { useSearchParams } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useCtemSignal, type CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { useFormatter } from "@/i18n";
import { isNotFoundError } from "@/lib/api";
import { formatDayIntl, formatShortDateTimeIntl } from "@/lib/relative-time";
import { CTEM_PRODUCT_INFO } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { DecisionReason, decisionSentence } from "./DecisionReason";
import { parseLoopParam, signalLoop, type LoopStageView } from "./loop-stages";

export const LOOP_PARAM = "loop";

const LENS_LABEL: Record<string, string> = { inside: "Inside", outside: "Outside", world: "World" };

function StageGlyph({ status }: { status: LoopStageView["status"] }) {
  if (status === "passed") return <Icon name="check" size={16} className="text-feedback-success-primary" />;
  if (status === "stopped") return <Icon name="ban" size={16} className="text-feedback-warning-primary" />;
  if (status === "current") return <Icon name="clock" size={16} className="text-accent" />;
  return <span className="h-1.5 w-1.5 rounded-full bg-elevation-hover ring-1 ring-[var(--border-elevation-subtle)]" aria-hidden />;
}

const STATUS_LABEL: Record<LoopStageView["status"], string> = {
  passed: "Passed",
  current: "Waiting",
  stopped: "Stopped",
  not_reached: "Not reached",
};

function SignalTrace({ signal }: { signal: CtemSignal }) {
  const { t_i18n, intl } = useFormatter();
  const stages = signalLoop(signal);
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 content-compact text-default-secondary">
        <span>{t_i18n(signal.trigger_path_label || signal.trigger_path)}</span>
        <span aria-hidden>·</span>
        <span>{t_i18n("Lens: {lens}", { lens: t_i18n(LENS_LABEL[signal.lens] ?? signal.lens) })}</span>
        <span aria-hidden>·</span>
        <span>{t_i18n("Source: {provider}", { provider: CTEM_PRODUCT_INFO[signal.source.provider]?.name ?? signal.source.provider })}</span>
      </div>
      <DecisionReason state={signal.state} decision={signal.decision} showInputs />
      {/* One vertical list at every width (#851 UX round 1 B1): the dialog is
          896 px at most, so a row of ten cells (81 px each) wraps its labels
          and staggers the status captions; the list keeps one baseline. */}
      <ol className="flex flex-col gap-0" aria-label={t_i18n("Loop stages")} data-testid="ctem-loop-stages">
        {stages.map((stage, index) => (
          <li
            key={stage.key}
            data-stage={stage.key}
            data-status={stage.status}
            className={cn(
              "flex min-w-0 items-center gap-2 rounded-sm px-2 py-1.5",
              (stage.status === "current" || stage.status === "stopped") && "layer-1 bg-elevation-hover",
              stage.status === "not_reached" && "text-default-secondary",
            )}
          >
            <span className="flex h-4 w-4 shrink-0 items-center justify-center">
              <StageGlyph status={stage.status} />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block content-compact font-medium">
                <span className="sr-only">{t_i18n("Stage {n}", { n: index + 1 })}: </span>
                {t_i18n(stage.label)}
              </span>
              <span className="block content-caption text-default-secondary">
                {t_i18n(STATUS_LABEL[stage.status])}
                {stage.at ? ` · ${formatShortDateTimeIntl(intl, stage.at)}` : ""}
                {/* The stage where the loop stopped names why (#851 UX round 2 B4). */}
                {stage.status === "stopped" ? ` · ${decisionSentence(signal.decision, t_i18n, (iso) => formatDayIntl(intl, iso))}` : ""}
              </span>
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function SignalTraceBody({ id }: { id: string }) {
  const { t_i18n } = useFormatter();
  const { data, error, isLoading } = useCtemSignal(id);
  if (isLoading) return <p className="content-compact text-default-secondary">{t_i18n("Loading...")}</p>;
  if (error && isNotFoundError(error)) return <p className="content-compact text-default-secondary">{t_i18n("This item is not available to you")}</p>;
  if (!data) return <p className="content-compact text-default-secondary">{t_i18n("This item is not available to you")}</p>;
  return <SignalTrace signal={data} />;
}

/**
 * Mount once on a page that holds loop rows: it opens itself when the URL
 * carries `?loop=`. A `case:` trace arrives with CTEM-03 (cases from
 * signals); until then it says so instead of drawing stages it cannot know.
 */
export function LoopTraceDialog() {
  const { t_i18n } = useFormatter();
  const [params, setParams] = useSearchParams();
  const target = parseLoopParam(params.get(LOOP_PARAM));
  const close = () => {
    const next = new URLSearchParams(params);
    next.delete(LOOP_PARAM);
    setParams(next, { replace: true });
  };
  return (
    <Dialog open={target !== null} onOpenChange={(open) => !open && close()}>
      <DialogContent className="max-w-4xl" data-testid="ctem-loop-trace">
        <DialogHeader>
          <DialogTitle>{t_i18n("Loop trace")}</DialogTitle>
          <DialogDescription>{t_i18n("Where this item is in the exposure management loop, and why.")}</DialogDescription>
        </DialogHeader>
        {target?.kind === "signal" ? (
          <SignalTraceBody id={target.id} />
        ) : target ? (
          <p className="content-compact text-default-secondary">{t_i18n("The loop trace of a case arrives with cases opened from signals.")}</p>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
