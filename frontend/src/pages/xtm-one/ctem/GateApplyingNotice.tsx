// SPDX-License-Identifier: Apache-2.0

/**
 * The "being applied" state of a gate answered on the case page (#2746), in
 * place of its decision card while the case still reads the gate waiting: a DS
 * spinner with the option chosen, the case and its runs refetched with backoff
 * for `APPLY_WINDOW_MS`; past it a calm line with Refresh; a failed run started
 * after the answer, said with a button to the run. No waiting hint, no link to
 * the decision, no wait timer. `useCollapseOut` keeps a block on the page for a
 * short height and opacity collapse once it goes (instant under
 * prefers-reduced-motion).
 */
import { useEffect, useState, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Icon, Spinner } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { useCtemCaseRuns } from "@/hooks/api/useCtemCasesApi";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { usePrefersReducedMotion } from "@/hooks/usePrefersReducedMotion";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { COLLAPSE_MS, applyPollDelay, gateApplying, recordGateAnswer, type GateAnswer } from "./gate-applying";

/** Where an answered gate stands, live: the case's own runs, re-read while it is being applied. */
function useGateApplyingState(waiting: boolean, answer: GateAnswer | null) {
  const { id } = useParams<{ id: string }>();
  const licensed = useAgenticAIEnabled();
  const { data: caseRuns } = useCtemCaseRuns(id, licensed && waiting && !!answer);
  const [now, setNow] = useState(() => Date.now());
  const state = gateApplying({ waiting, answer, runs: caseRuns?.runs, now });
  const qc = useQueryClient();
  const phase = state?.phase ?? null;
  const since = state?.since ?? null;
  useEffect(() => {
    if (phase !== "applying" || since == null) return;
    const delay = applyPollDelay(Date.now() - since);
    const timer = window.setTimeout(() => {
      void qc.invalidateQueries({ queryKey: ["ctem-cases", "case"] });
      void qc.invalidateQueries({ queryKey: ["ctem-cases", "runs"] });
      setNow(Date.now());
    }, delay ?? 0);
    return () => window.clearTimeout(timer);
  }, [phase, since, now, qc]);
  return state;
}

export function GateApplyingNotice({
  workItemId,
  waiting,
  answer,
  testId,
}: {
  workItemId: string;
  waiting: boolean;
  answer: GateAnswer | null;
  testId: string;
}) {
  const { t_i18n, t_text } = useFormatter();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const state = useGateApplyingState(waiting, answer);
  if (!state) return null;
  const option = state.label ? t_text(state.label) : null;

  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2" role="status" aria-live="polite" data-testid={testId} data-phase={state.phase}>
      {state.phase === "applying" && (
        <>
          <Spinner size="sm" label={t_i18n("Loading")} />
          <span className="min-w-0">
            {option
              ? t_i18n("Decision recorded: {option}. The loop is applying it.", { option })
              : t_i18n("Decision recorded. The loop is applying it.")}
          </span>
        </>
      )}
      {state.phase === "timeout" && (
        <>
          <span className="min-w-0 text-default-secondary">{t_i18n("Recorded, still being applied. Refresh later.")}</span>
          <Button
            variant="outline"
            size="sm"
            className="gap-1.5"
            data-testid={`${testId}-refresh`}
            onClick={() => {
              // A new window of polling from now, on the same answer.
              recordGateAnswer(workItemId, state.label);
              void qc.invalidateQueries({ queryKey: ["ctem-cases", "case"] });
              void qc.invalidateQueries({ queryKey: ["ctem-cases", "runs"] });
            }}
          >
            <Icon name="refresh-cw" size={16} aria-hidden />
            {t_i18n("Refresh")}
          </Button>
        </>
      )}
      {state.phase === "failed" && (
        <>
          <Icon name="circle-alert" size={16} className="shrink-0 text-feedback-error-primary" aria-hidden />
          <span className="min-w-0">{t_i18n("Decision recorded, but the run applying it failed.")}</span>
          <Button variant="outline" size="sm" className="gap-1.5" data-testid={`${testId}-run`} onClick={() => navigate(`/operations/executions/${state.runId}`)}>
            <Icon name="external-link" size={16} aria-hidden />
            {t_i18n("Open the run")}
          </Button>
        </>
      )}
    </div>
  );
}

/**
 * Keep a block rendered for `COLLAPSE_MS` once `show` turns false, so it can
 * collapse; `closing` is true during that time. Under prefers-reduced-motion it
 * goes at once.
 */
export function useCollapseOut(show: boolean): { render: boolean; closing: boolean } {
  const reduced = usePrefersReducedMotion();
  const [shown, setShown] = useState(show);
  const [closing, setClosing] = useState(false);
  useEffect(() => {
    if (show) {
      setShown(true);
      setClosing(false);
      return;
    }
    if (!shown) return;
    if (reduced) {
      setShown(false);
      return;
    }
    setClosing(true);
    const timer = window.setTimeout(() => {
      setShown(false);
      setClosing(false);
    }, COLLAPSE_MS);
    return () => window.clearTimeout(timer);
  }, [show, shown, reduced]);
  return { render: show || shown, closing: !show && closing };
}

/** The collapsing frame of `useCollapseOut`: a height and opacity transition, none under prefers-reduced-motion. */
export function CollapseFrame({ closing, children, testId }: { closing: boolean; children: ReactNode; testId?: string }) {
  return (
    <div
      className={cn(
        "grid transition-[grid-template-rows,opacity] duration-300 ease-out motion-reduce:transition-none",
        closing ? "grid-rows-[0fr] opacity-0" : "grid-rows-[1fr] opacity-100",
      )}
      aria-hidden={closing || undefined}
      data-testid={testId}
      data-closing={closing ? "true" : undefined}
    >
      <div className="min-h-0 overflow-hidden">{children}</div>
    </div>
  );
}
