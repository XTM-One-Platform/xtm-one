// SPDX-License-Identifier: Apache-2.0

/**
 * Purge every signal, administrators only (#2528, dev-docs/ctem-signals.md
 * section 6ter): the destructive icon button next to Sources on the Signals page.
 * The confirm dialog reads the live counts first (GET /ctem-signals/purge), says it
 * cannot be undone and that push events received during the job are not recorded,
 * offers the resync (unchecked by default) and enables only once the shown code is
 * typed back. The job then runs on the server; the progress dialog follows it step
 * by step (pause, purge, resync, resume). A non-administrator never sees it.
 */
import { useState } from "react";
import { Checkbox, Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import {
  useCtemSignalPurge,
  useStartCtemSignalPurge,
  type CtemSignalPurgeJob,
  type CtemSignalPurgeStepState,
} from "@/hooks/api/useCtemSignalsApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

const STEP_ORDER = ["pause", "purge", "resync", "resume"] as const;

const STEP_GLYPH: Record<CtemSignalPurgeStepState, { name: "circle" | "loader-circle" | "circle-check" | "circle-x" | "ban"; tone: string }> = {
  pending: { name: "circle", tone: "text-default-secondary" },
  running: { name: "loader-circle", tone: "text-accent animate-spin" },
  done: { name: "circle-check", tone: "text-feedback-success-primary" },
  failed: { name: "circle-x", tone: "text-feedback-error-primary" },
  skipped: { name: "ban", tone: "text-default-secondary" },
};

function num(value: unknown): number {
  return typeof value === "number" ? value : 0;
}

export function SignalsPurgeAction() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [progressOpen, setProgressOpen] = useState(false);
  const [resync, setResync] = useState(false);
  const info = useCtemSignalPurge(isAdmin && (confirmOpen || progressOpen));
  const start = useStartCtemSignalPurge();

  if (!isAdmin) return null;

  const job = info.data?.job ?? null;
  const running = job?.state === "running";
  const counts = info.data?.counts;

  const ask = () => {
    if (running) {
      setProgressOpen(true);
      return;
    }
    setResync(false);
    setConfirmOpen(true);
    void info.refetch();
  };

  const confirm = () =>
    start.mutate(
      { confirm: info.data?.confirm_word ?? "", resync },
      {
        onSuccess: () => setProgressOpen(true),
        // The server's reason (a job already running, the word refused), never a silent failure.
        onError: (err) => toast({ title: t_i18n("Purge not started"), description: err.message, variant: "error" }),
      },
    );

  const stepLabel: Record<(typeof STEP_ORDER)[number], string> = {
    pause: t_i18n("Pause signal intake"),
    purge: t_i18n("Delete the signals and their cases"),
    resync: t_i18n("Resync from the sources"),
    resume: t_i18n("Resume signal intake"),
  };

  const stepDetail = (step: (typeof STEP_ORDER)[number], current: CtemSignalPurgeJob): string | null => {
    const c = current.steps[step].counts;
    if (step === "purge" && (c.signals !== undefined || c.cases !== undefined)) {
      return t_i18n("{signals, plural, one {# signal} other {# signals}}, {cases, plural, one {# case} other {# cases}}", {
        signals: num(c.signals),
        cases: num(c.cases),
      });
    }
    if (step === "resync" && c.paths && typeof c.paths === "object") {
      const total = Object.values(c.paths as Record<string, unknown>).reduce<number>((sum, n) => sum + num(n), 0);
      return t_i18n("{count, plural, one {# signal recorded} other {# signals recorded}}", { count: total });
    }
    return null;
  };

  return (
    <>
      <Tip content={t_i18n("Purge all signals")}>
        <Button
          variant="ghost"
          size="icon"
          className="text-feedback-error-primary hover:bg-feedback-error-primary/10 hover:text-feedback-error-primary"
          aria-label={t_i18n("Purge all signals")}
          onClick={ask}
          data-testid="ctem-signals-purge"
        >
          <Icon name="trash-2" size={16} aria-hidden />
        </Button>
      </Tip>
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={t_i18n("Purge all signals?")}
        description={t_i18n(
          "Every signal and every case a signal opened or joined are permanently deleted, with their history and open work items. Sources and their settings stay. This cannot be undone.",
        )}
        confirmLabel={t_i18n("Purge")}
        challenge
        confirmDisabled={!counts || start.isPending}
        onConfirm={confirm}
      >
        <div className="space-y-3 content-compact" aria-busy={!counts && !info.isError} data-testid="ctem-signals-purge-preview">
          {info.isError ? (
            <p className="text-feedback-error-primary" role="alert">
              {info.error.message}
            </p>
          ) : !counts ? (
            <p className="text-default-secondary">{t_i18n("Counting what the purge deletes")}</p>
          ) : (
            <ul className="list-disc space-y-0.5 pl-5">
              <li>{t_i18n("{count, plural, one {# signal} other {# signals}}", { count: counts.signals })}</li>
              <li>{t_i18n("{count, plural, one {# case} other {# cases}}", { count: counts.cases })}</li>
            </ul>
          )}
          <p className="text-default-secondary">
            {t_i18n("Signal intake is paused while the purge runs: events pushed by the sources meanwhile are not recorded.")}
          </p>
          <Checkbox
            id="ctem-signals-purge-resync"
            label={t_i18n("Resync signals from sources after the purge")}
            checked={resync}
            onCheckedChange={(checked) => setResync(checked === true)}
          />
          {/* What refills the list after the purge (#2583 note): the recompute is held without a resync. */}
          <p className="text-default-secondary" data-testid="ctem-signals-purge-recompute">
            {resync
              ? t_i18n("The resync reads every source again, with the paths and sources that are on.")
              : t_i18n("Without a resync, the cases recompute is held for one interval: the list stays empty until you resync or run a recompute.")}
          </p>
        </div>
      </ConfirmDialog>
      <Dialog open={progressOpen} onOpenChange={setProgressOpen}>
        <DialogContent className="max-w-sm" data-testid="ctem-signals-purge-progress">
          <DialogHeader>
            <DialogTitle className="text-base">
              {running || !job
                ? t_i18n("Purging all signals")
                : job.interrupted
                  ? t_i18n("Purge interrupted")
                  : job.state === "failed"
                    ? t_i18n("Purge failed")
                    : t_i18n("Purge finished")}
            </DialogTitle>
            <DialogDescription className="content-base leading-relaxed">
              {running || !job
                ? t_i18n("You can close this dialog: the purge goes on.")
                : job.interrupted
                  ? `${t_i18n("The purge stopped before its end because its process restarted. You can start a new purge.")} ${
                      job.intake_paused ? t_i18n("Signal intake is still paused.") : t_i18n("Signal intake is running again.")
                    }`
                  : (job.error ?? t_i18n("Signal intake is running again."))}
            </DialogDescription>
          </DialogHeader>
          {job && (
            <ol className="space-y-2 content-compact">
              {STEP_ORDER.map((step) => {
                const state = job.steps[step].state;
                const glyph = STEP_GLYPH[state];
                const detail = stepDetail(step, job);
                return (
                  <li key={step} className="flex items-start gap-2" data-testid={`ctem-signals-purge-step-${step}`} data-state={state}>
                    <Icon name={glyph.name} size={16} aria-hidden className={cn("mt-0.5 shrink-0", glyph.tone)} />
                    <span>
                      <span className={cn(state === "skipped" && "text-default-secondary")}>{stepLabel[step]}</span>
                      {detail && <span className="block text-default-secondary tabular-nums">{detail}</span>}
                    </span>
                  </li>
                );
              })}
            </ol>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
