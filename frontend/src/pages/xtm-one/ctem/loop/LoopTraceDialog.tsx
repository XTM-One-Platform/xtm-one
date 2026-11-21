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
import type { ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  CTEM_SIGNAL_STATES, useCtemSignal, useCtemSignalList, type CtemSignal, type CtemSignalState,
} from "@/hooks/api/useCtemSignalsApi";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { isNotFoundError } from "@/lib/api";
import { formatDayIntl, formatShortDateTimeIntl } from "@/lib/relative-time";
import { CTEM_PRODUCT_INFO } from "@/lib/constants";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { cn, safeExternalUrl } from "@/lib/utils";
import { DecisionReason, decisionSentence, SIGNAL_STATE_LABEL } from "./DecisionReason";
import { loopParam, parseLoopParam, signalLoop, signalsParam, type LoopStageView } from "./loop-stages";

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

/**
 * The evidence keys that are the link to the source object, not a fact: `link`
 * (OpenCTI, Aikido) and `platform_url` (an OpenAEV delivery, CTEM-15-UI #956:
 * the scenario that regressed or the run that proved a path).
 */
const SOURCE_LINK_KEYS = ["link", "platform_url"] as const;

/** The link to the source object a signal's evidence carries, if any. */
export function signalSourceLink(evidence: Record<string, unknown> | null | undefined): string | undefined {
  for (const key of SOURCE_LINK_KEYS) {
    const value = evidence?.[key];
    if (typeof value === "string" && value) return value;
  }
  return undefined;
}

/** An evidence value in words: a scalar as it is, a list joined, anything else left out. */
function evidenceText(value: unknown): string {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value) && value.every((v) => typeof v === "string" || typeof v === "number")) return value.join(", ");
  return "";
}

/**
 * What the signal is, before where it is in the loop (CTEM-01-UI): its
 * subject, its source with the link to the source object, when it was first
 * and last seen, how many times, and the evidence the source gave.
 */
function SignalFacts({ signal }: { signal: CtemSignal }) {
  const { t_i18n, intl } = useFormatter();
  const link = safeExternalUrl(signalSourceLink(signal.evidence));
  const provider = CTEM_PRODUCT_INFO[signal.source.provider]?.name ?? signal.source.provider;
  const evidence = Object.entries(signal.evidence ?? {})
    .filter(([key]) => !(SOURCE_LINK_KEYS as readonly string[]).includes(key))
    .map(([key, value]) => ({ key, value: evidenceText(value) }))
    .filter((row) => row.value)
    .slice(0, 8);
  const when = (iso: string | null) => (iso ? formatShortDateTimeIntl(intl, iso) : "--");
  const rows: { label: string; value: ReactNode }[] = [
    { label: t_i18n("Subject"), value: signal.subject.label || signal.subject.ref },
    {
      label: t_i18n("Source"),
      value: link ? (
        <a href={link} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-accent hover:underline">
          {provider}
          <Icon name="external-link" size={16} className="shrink-0" />
          <span className="sr-only">{t_i18n("Open the source")}</span>
        </a>
      ) : (
        provider
      ),
    },
    { label: t_i18n("First seen"), value: when(signal.first_seen_at) },
    { label: t_i18n("Last seen"), value: when(signal.last_seen_at) },
    { label: t_i18n("Seen"), value: t_i18n("{count, plural, one {# time} other {# times}}", { count: signal.seen_count }) },
  ];
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 content-caption" data-testid="ctem-signal-facts">
      {rows.map((row) => (
        <div key={row.label} className="contents">
          <dt className="text-default-secondary">{row.label}</dt>
          <dd className="min-w-0 truncate">{row.value}</dd>
        </div>
      ))}
      {evidence.length > 0 && (
        <div className="contents">
          <dt className="text-default-secondary">{t_i18n("Evidence")}</dt>
          <dd className="min-w-0" data-testid="ctem-signal-evidence">
            {evidence.map((row) => (
              <span key={row.key} className="block truncate" title={row.value}>
                <span className="text-default-secondary">{row.key}: </span>
                {row.value}
              </span>
            ))}
          </dd>
        </div>
      )}
    </dl>
  );
}

function SignalTrace({ signal }: { signal: CtemSignal }) {
  const { t_i18n, intl } = useFormatter();
  const stages = signalLoop(signal);
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 content-compact text-default-secondary">
        <span className="font-medium text-default-primary">{t_i18n(signal.trigger_path_label || signal.trigger_path)}</span>
        <span aria-hidden>·</span>
        <span>{t_i18n("Lens: {lens}", { lens: t_i18n(LENS_LABEL[signal.lens] ?? signal.lens) })}</span>
      </div>
      <SignalFacts signal={signal} />
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
 * The signals in one state (or every state), newest sighting first, each
 * opening its own trace (CTEM-01-UI). The trigger path leads every row: a path
 * with no CVE and no actor reads exactly like a vulnerability.
 */
function SignalListBody({ state, onState, onOpen }: {
  state: CtemSignalState | null;
  onState: (state: CtemSignalState | null) => void;
  onOpen: (id: string) => void;
}) {
  const { t_i18n, intl } = useFormatter();
  const { count: formatCount, exactCount } = useNumberFormat();
  const { data, isLoading, hasNextPage, fetchNextPage, isFetchingNextPage } = useCtemSignalList(state);
  const first = data?.pages[0];
  const counts = first?.counts ?? {};
  const total = first?.total ?? 0;
  const all = CTEM_SIGNAL_STATES.reduce((sum, s) => sum + (counts[s] ?? 0), 0);
  const filters: { key: CtemSignalState | null; label: string; count: number }[] = [
    { key: null, label: t_i18n("All"), count: all },
    ...CTEM_SIGNAL_STATES.map((s) => ({ key: s, label: t_i18n(SIGNAL_STATE_LABEL[s]), count: counts[s] ?? 0 })),
  ];
  const signals = data?.pages.flatMap((page) => page.signals) ?? [];
  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid="ctem-signal-list">
      {/* The filters and the "n of N" count stay in view: the list below is the
          only scroller (#877 UX round 1 B2). */}
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5">
      <div role="group" aria-label={t_i18n("Filter by state")} className="flex flex-wrap gap-1.5">
        {filters.map((f) => (
          <button
            key={f.key ?? "all"}
            type="button"
            aria-pressed={f.key === state}
            onClick={() => onState(f.key)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-sm px-2 py-1 content-caption",
              BUTTON_ROLE_FOCUS_RING,
              f.key === state ? "bg-elevation-hover font-medium" : "text-default-secondary hover:bg-elevation-hover",
            )}
          >
            {f.label}
            <span className="tabular-nums" title={exactCount(f.count)}>
              {formatCount(f.count)}
            </span>
          </button>
        ))}
      </div>
      {total > 0 && (
        <span className="ml-auto shrink-0 content-caption text-default-secondary tabular-nums" data-testid="ctem-signal-shown">
          {t_i18n("Showing {shown} of {total}", { shown: formatCount(signals.length), total: formatCount(total) })}
        </span>
      )}
      </div>
      {isLoading ? (
        <p className="content-compact text-default-secondary">{t_i18n("Loading...")}</p>
      ) : signals.length === 0 ? (
        <p className="content-compact text-default-secondary">{t_i18n("No signal in this state yet")}</p>
      ) : (
        <ul className="flex max-h-[min(55vh,32rem)] flex-col overflow-y-auto" data-testid="ctem-signal-rows">
          {signals.map((s) => (
            <li key={s.id}>
              <button
                type="button"
                onClick={() => onOpen(s.id)}
                data-testid="ctem-signal-row"
                data-path={s.trigger_path}
                className={cn("flex w-full min-w-0 flex-col gap-1 rounded-sm px-2 py-1.5 text-left hover:bg-elevation-hover", BUTTON_ROLE_FOCUS_RING)}
              >
                <span className="flex min-w-0 items-baseline gap-2 content-compact">
                  <span className="shrink-0 font-medium">{t_i18n(s.trigger_path_label || s.trigger_path)}</span>
                  <span className="min-w-0 truncate text-default-secondary">{s.subject.label || s.subject.ref}</span>
                  <span className="ml-auto shrink-0 content-caption text-default-secondary">
                    {s.last_seen_at ? formatShortDateTimeIntl(intl, s.last_seen_at) : ""}
                  </span>
                </span>
                <DecisionReason state={s.state} decision={s.decision} />
              </button>
            </li>
          ))}
          {hasNextPage && (
            <li>
              <button
                type="button"
                onClick={() => void fetchNextPage()}
                disabled={isFetchingNextPage}
                data-testid="ctem-signal-load-more"
                className={cn("w-full rounded-sm px-2 py-1.5 text-left content-compact text-accent hover:bg-elevation-hover disabled:text-default-secondary", BUTTON_ROLE_FOCUS_RING)}
              >
                {isFetchingNextPage ? t_i18n("Loading...") : t_i18n("Load more")}
              </button>
            </li>
          )}
        </ul>
      )}
    </div>
  );
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
  const go = (value: string, replace = false) => {
    const next = new URLSearchParams(params);
    next.set(LOOP_PARAM, value);
    setParams(next, { replace });
  };
  return (
    <Dialog open={target !== null} onOpenChange={(open) => !open && close()}>
      <DialogContent className="max-w-4xl" data-testid="ctem-loop-trace">
        <DialogHeader>
          <DialogTitle>{t_i18n("Loop trace")}</DialogTitle>
          <DialogDescription>{t_i18n("Where this item is in the exposure management loop, and why.")}</DialogDescription>
        </DialogHeader>
        {target?.kind === "signals" ? (
          <SignalListBody
            state={target.state}
            onState={(state) => go(signalsParam(state), true)}
            onOpen={(id) => go(loopParam("signal", id))}
          />
        ) : target?.kind === "signal" ? (
          <SignalTraceBody id={target.id} />
        ) : target ? (
          <p className="content-compact text-default-secondary">{t_i18n("The loop trace of a case arrives with cases opened from signals.")}</p>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
