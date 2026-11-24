// SPDX-License-Identifier: Apache-2.0

/**
 * The loop trace side sheet (dev-docs/ctem-loop-visibility.md section 4): one
 * signal's place in the exposure management loop, in a panel over the page.
 * One component for every page that opens a trace: the Signals list and the
 * case page's "Opened by" card (Sam, 24 Nov 2026: the trace opens on the case,
 * never by leaving it). The open signal lives in the page's own URL (`signal`),
 * so a reload reopens it and Back closes it; `useLoopTraceParam` reads and
 * writes that parameter on whichever route renders the sheet.
 */
import { useSearchParams, Link as RouterLink } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { SlideOver } from "@/components/ui/slide-over";
import { useFormatter } from "@/i18n";
import { SignalTraceBody } from "./SignalLoopTrace";
import { SIGNALS_PARAMS, signalsHref } from "./signals-route";

/**
 * The open loop trace of the current route: its signal id and the writers that
 * open and close it. Both push a history entry, so Back undoes the last one.
 */
export function useLoopTraceParam() {
  const [params, setParams] = useSearchParams();
  const signal = params.get(SIGNALS_PARAMS.signal) || null;
  const set = (value: string | null) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set(SIGNALS_PARAMS.signal, value);
        else next.delete(SIGNALS_PARAMS.signal);
        return next;
      },
      { preventScrollReset: true },
    );
  return { signal, open: (id: string) => set(id), close: () => set(null) };
}

export function LoopTraceSheet({
  signalId,
  onClose,
  showOpenInSignals = false,
  currentCaseId = null,
}: {
  /** The signal whose trace is open; null keeps the sheet closed. */
  signalId: string | null;
  onClose: () => void;
  /** Off the Signals page: a secondary link to the trace in the full list, the only way out of the page. */
  showOpenInSignals?: boolean;
  /** The case whose page renders the sheet: the trace draws no "Open case" link to it. */
  currentCaseId?: string | null;
}) {
  const { t_i18n } = useFormatter();
  return (
    <SlideOver open={!!signalId} onClose={onClose} label={t_i18n("Loop trace")} width="w-[min(560px,45vw)]">
      <div className="flex h-full min-h-0 flex-col" data-testid="ctem-loop-trace">
        <div className="flex items-start justify-between gap-3 border-b border-elevation-subtle p-4">
          <div className="flex min-w-0 flex-col gap-1">
            <h2 className="title-sm">{t_i18n("Loop trace")}</h2>
            <p className="content-compact text-default-secondary">{t_i18n("Where this signal is in the exposure management loop, and why.")}</p>
            {showOpenInSignals && signalId && (
              <Button asChild variant="link" size="sm" className="h-auto self-start gap-1 p-0">
                <RouterLink to={signalsHref({ signal: signalId })} data-testid="ctem-loop-trace-open-in-signals">
                  {t_i18n("Open in Signals")}
                  <Icon name="arrow-right" size={16} aria-hidden />
                </RouterLink>
              </Button>
            )}
          </div>
          <Button variant="ghost" size="sm" onClick={onClose} data-testid="ctem-loop-trace-close">
            <Icon name="x" size={16} aria-hidden />
            {t_i18n("Close")}
          </Button>
        </div>
        {/* The sheet's only scroller: the trace body. */}
        <div className="min-h-0 flex-1 overflow-y-auto p-4" data-testid="ctem-loop-trace-scroll">
          {signalId && <SignalTraceBody id={signalId} currentCaseId={currentCaseId} />}
        </div>
      </div>
    </SlideOver>
  );
}
