// SPDX-License-Identifier: Apache-2.0

/**
 * Purge every signal, administrators only (#2528, dev-docs/ctem-signals.md
 * section 6ter): the destructive icon button next to Sources on the Signals page.
 * The confirm dialog reads the live counts first (GET /ctem-signals/purge), says it
 * cannot be undone and that push events received during the job are not recorded,
 * offers the resync (unchecked by default) and enables only once the shown code is
 * typed back. The job then runs on the server; the progress dialog follows it step
 * by step (pause, purge, resync, resume). A non-administrator never sees it.
 *
 * #2578: with the resync ticked, one window per route (each OpenCTI type, the PIR
 * matches, third-party events, the inside sources), its default from the server;
 * a pass reads the current state. The finished job lists the outcome of each route.
 */
import { useState } from "react";
import { Checkbox, Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import {
  useCtemSignalPurge,
  useStartCtemSignalPurge,
  type CtemSignalPurgeJob,
  type CtemSignalPurgeStepState,
  type CtemSignalResyncRoute,
  type CtemSignalResyncRouteResult,
} from "@/hooks/api/useCtemSignalsApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { initialWindowText, parseWindow, routeOutcome, windowsToSend } from "./purge-resync-windows";

const GROUP_HEADING: Record<CtemSignalResyncRoute["group"], string> = {
  world: "Outside world (OpenCTI)",
  inside: "Inside state",
  pass: "Scheduled passes",
};

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
  const [windowText, setWindowText] = useState<Record<string, string>>({});
  const info = useCtemSignalPurge(isAdmin && (confirmOpen || progressOpen));
  const start = useStartCtemSignalPurge();

  if (!isAdmin) return null;

  const job = info.data?.job ?? null;
  const running = job?.state === "running";
  const counts = info.data?.counts;
  const routes = info.data?.resync_routes ?? [];
  const maxDays = info.data?.resync_max_days ?? 365;
  const windows = resync ? windowsToSend(routes, { ...initialWindowText(routes), ...windowText }, maxDays) : null;

  const ask = () => {
    if (running) {
      setProgressOpen(true);
      return;
    }
    setResync(false);
    setWindowText({});
    setConfirmOpen(true);
    void info.refetch();
  };

  const confirm = () =>
    start.mutate(
      { confirm: info.data?.confirm_word ?? "", resync, ...(resync && windows ? { resync_windows: windows } : {}) },
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
        size={resync ? "wide" : "sm"}
        confirmDisabled={!counts || start.isPending || (resync && !windows)}
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
          {resync && routes.length > 0 && (
            <ResyncWindows
              routes={routes}
              maxDays={maxDays}
              texts={{ ...initialWindowText(routes), ...windowText }}
              onChange={(key, text) => setWindowText((prev) => ({ ...prev, [key]: text }))}
            />
          )}
        </div>
      </ConfirmDialog>
      <Dialog open={progressOpen} onOpenChange={setProgressOpen}>
        <DialogContent className="max-w-md" data-testid="ctem-signals-purge-progress">
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
          {job && !running && <ResyncResult job={job} routes={routes} />}
        </DialogContent>
      </Dialog>
    </>
  );
}

/** One window field per route, grouped; a pass says it reads the current state (#2578). */
function ResyncWindows({
  routes,
  maxDays,
  texts,
  onChange,
}: {
  routes: CtemSignalResyncRoute[];
  maxDays: number;
  texts: Record<string, string>;
  onChange: (key: string, text: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const groups = (Object.keys(GROUP_HEADING) as CtemSignalResyncRoute["group"][]).filter((g) => routes.some((r) => r.group === g));
  return (
    <div className="space-y-3" data-testid="ctem-signals-purge-windows">
      <p className="text-default-secondary">
        {t_i18n("How far back the resync reads, in days. Leave an inside-state field empty to read every open item.")}
      </p>
      {groups.map((group) => (
        <fieldset key={group} className="space-y-1.5">
          <legend className="font-medium">{t_i18n(GROUP_HEADING[group])}</legend>
          {/* Two columns in the wide dialog, no inner scroller: every field and its error stay in view (UX round 1 of #2590, B1 and B2). */}
          <div className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2">
            {routes
              .filter((r) => r.group === group)
              .map((route) => {
                const id = `ctem-signals-purge-window-${route.key}`;
                if (!route.windowed) {
                  // A pass, or a route the resync does not read: a read-only line with its reason, never a field (B3).
                  return (
                    <div key={route.key} className="min-w-0" data-testid={id}>
                      <span className="block">{t_i18n(route.label)}</span>
                      <span className="block text-default-secondary">{t_i18n(route.note ?? "Runs once")}</span>
                    </div>
                  );
                }
                const invalid = parseWindow(texts[route.key] ?? "", route, maxDays) === "invalid";
                return (
                  <div key={route.key} className="min-w-0">
                    <label id={`${id}-label`} htmlFor={id} className="block">
                      {t_i18n(route.label)}
                    </label>
                    <span className="mt-1 flex items-center gap-1.5">
                      <Input
                        id={id}
                        inputMode="numeric"
                        className="h-8 w-20 shrink-0 [&_input]:text-right [&_input]:tabular-nums"
                        value={texts[route.key] ?? ""}
                        placeholder={route.group === "inside" ? t_i18n("All") : undefined}
                        aria-invalid={invalid}
                        aria-labelledby={`${id}-label`}
                        aria-describedby={invalid ? `${id}-error` : undefined}
                        onChange={(e) => onChange(route.key, e.target.value)}
                      />
                      <span className="shrink-0 whitespace-nowrap text-default-secondary">{t_i18n("days")}</span>
                    </span>
                    {invalid && (
                      <p id={`${id}-error`} className="mt-1 text-feedback-error-primary" role="alert">
                        {t_i18n("Enter a whole number from {min} to {max}", { min: 1, max: maxDays })}
                      </p>
                    )}
                  </div>
                );
              })}
          </div>
        </fieldset>
      ))}
    </div>
  );
}

/** What the resync read and recorded on each route, at the end of the progress dialog (#2578). */
function ResyncResult({ job, routes }: { job: CtemSignalPurgeJob; routes: CtemSignalResyncRoute[] }) {
  const { t_i18n } = useFormatter();
  const results = job.steps.resync.counts.routes as Record<string, CtemSignalResyncRouteResult> | undefined;
  if (!results || typeof results !== "object") return null;
  const labels = new Map(routes.map((r) => [r.key, r.label]));
  return (
    <div className="max-h-64 overflow-y-auto border-t border-line pt-3" data-testid="ctem-signals-purge-result">
      <p className="mb-1.5 font-medium content-compact">{t_i18n("Resync by source")}</p>
      <ul className="space-y-1.5 content-compact">
        {Object.entries(results).map(([key, result]) => {
          const outcome = routeOutcome(result);
          return (
            <li key={key} className="flex items-start justify-between gap-3" data-testid={`ctem-signals-purge-result-${key}`} data-kind={outcome.kind}>
              <span className="min-w-0">
                <span className={cn(outcome.kind === "skipped" && "text-default-secondary")}>{t_i18n(labels.get(key) ?? key)}</span>
                {outcome.kind !== "done" && (
                  <span className={cn("block", outcome.kind === "error" ? "text-feedback-error-primary" : "text-default-secondary")}>
                    {outcome.reason}
                  </span>
                )}
                {outcome.kind === "done" && outcome.capped && (
                  <span className="block text-default-secondary">{t_i18n("Stopped at the read limit")}</span>
                )}
              </span>
              {outcome.kind !== "skipped" && (
                <span className="shrink-0 text-right tabular-nums text-default-secondary">
                  {t_i18n("{read} read, {recorded} recorded", { read: outcome.read, recorded: outcome.recorded })}
                </span>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
