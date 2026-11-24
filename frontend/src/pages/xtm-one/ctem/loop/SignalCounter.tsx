// SPDX-License-Identifier: Apache-2.0

/**
 * The signals counter of the command center (CTEM-01-UI,
 * dev-docs/ctem-loop-visibility.md sections 2 and 6): one compact line in the
 * existing exposure lifecycle card, the signals by state, each count opening
 * the loop trace on the signals of that state (`/ctem/signals?state=<state>`). The
 * counts are the API's own (`counts` of `GET /api/v1/ctem-signals`), refreshed
 * by the activity stream frame `ctem_signal_update`, so a recorded signal moves
 * its count without a reload. A state with no signal keeps its zero: the loop
 * is never hidden.
 */
import { Icon } from "@filigran/design-system";
import { Tip } from "@/components/ui/tooltip";
import type { CtemSignalState } from "@/hooks/api/useCtemSignalsApi";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { cn } from "@/lib/utils";
import { SIGNAL_STATE_LABEL } from "./DecisionReason";

/** The states the counter names, in loop order; `confirmed` is the step between reconcile and the gate, counted under All. */
const COUNTED_SIGNAL_STATES: readonly CtemSignalState[] = ["new", "to_validate", "watchlist", "discarded", "case_opened"];

export function SignalCounter({
  counts,
  onOpen,
}: {
  counts: Partial<Record<CtemSignalState, number>>;
  /** `null` opens every signal. */
  onOpen: (state: CtemSignalState | null) => void;
}) {
  const { t_i18n } = useFormatter();
  const { count: formatCount, exactCount } = useNumberFormat();
  const total = Object.values(counts).reduce<number>((sum, n) => sum + (n ?? 0), 0);
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-1 content-caption" data-testid="ctem-signal-counter">
      <button
        type="button"
        onClick={() => onOpen(null)}
        aria-label={t_i18n("Open all signals")}
        className={cn("inline-flex items-center gap-1.5 rounded-sm px-1.5 py-0.5 font-medium hover:bg-elevation-hover", BUTTON_ROLE_FOCUS_RING)}
      >
        <Icon name="activity" size={16} className="shrink-0 text-accent" aria-hidden />
        {t_i18n("Signals")}
        <span className="tabular-nums" title={exactCount(total)}>
          {formatCount(total)}
        </span>
      </button>
      {COUNTED_SIGNAL_STATES.map((state) => {
        const count = counts[state] ?? 0;
        const label = t_i18n(SIGNAL_STATE_LABEL[state]);
        // "Case opened" counts the signals that opened a case, all time (#1621): a case the vulnerability
        // readers built from your stack is not a signal, so it is counted in Cases, never here.
        const why = state === "case_opened" ? t_i18n("Signals that opened a case, all time. Cases built from your stack's vulnerabilities are counted in Cases.") : null;
        const button = (
          <button
            key={state}
            type="button"
            data-state={state}
            onClick={() => onOpen(state)}
            aria-label={t_i18n("Open the signals: {state}", { state: `${label} ${exactCount(count)}` })}
            className={cn(
              "inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 hover:bg-elevation-hover",
              BUTTON_ROLE_FOCUS_RING,
              count === 0 ? "text-default-secondary" : "text-default-primary",
            )}
          >
            <span className="tabular-nums font-medium" title={exactCount(count)}>
              {formatCount(count)}
            </span>
            <span>{label}</span>
          </button>
        );
        return why ? <Tip key={state} content={why}>{button}</Tip> : button;
      })}
    </div>
  );
}
