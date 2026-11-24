// SPDX-License-Identifier: Apache-2.0

/**
 * "Opened by" on a case page (CTEM-03-UI, #892; dev-docs/ctem-loop-visibility.md
 * sections 3 and 4): the signals that opened or joined the case. Read through
 * `GET /ctem-signals?case_id=`, which applies the reader's visibility. A case
 * is opened only by a signal or split by a person or an agent (#1529): a case
 * with no signal says which, and one a source read built before the readers
 * recorded signals is labelled legacy.
 *
 * Compact since #1679 point 6 (Sam, 24 Nov 2026): the signals are grouped by
 * trigger path, lens, source and scope decision (`opened-by-groups.ts`). A
 * group draws one line: the path, lens and source chips, a count chip, the
 * scope sentence once, the first and last seen days, the subjects as chips
 * ("+N more" past the first few) and the loop trace of its newest signal as a
 * button. A group of up to three signals keeps the per-signal detail; a larger
 * one opens ("Show all N") into a one-row-height table of its signals, each
 * with its own loop trace button, collapsed by default above five and
 * filterable above ten.
 */
import { useMemo, useState } from "react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ToneBadge } from "@/components/ui/tone-badge";
import { useCtemCaseSignals, type CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { useFormatter } from "@/i18n";
import { formatDayIntl } from "@/lib/relative-time";
import type { CtemCaseEvent } from "@/hooks/api/useCtemCasesApi";
import { NO_SIGNAL_ORIGIN_LABEL, noSignalOrigin } from "./case-loop-reason";
import { LoopTraceButton } from "./LoopTraceButton";
import { SIGNAL_STATE_LABEL, decisionSentence } from "./loop/DecisionReason";
import { signalProviderName } from "./loop/SignalLoopTrace";
import {
  OPENED_BY_COLLAPSE_ABOVE,
  OPENED_BY_DETAIL_MAX,
  OPENED_BY_FILTER_ABOVE,
  groupOpenedBySignals,
  signalSubject,
  type OpenedByGroup,
} from "./opened-by-groups";

const LENS_LABEL: Record<string, string> = { inside: "Inside", outside: "Outside", world: "World" };
/** Subjects drawn as chips on a group line before "+N more". */
const SUBJECT_CHIPS = 4;

export function CaseOpenedBy({ caseId, history }: { caseId: string; history?: readonly CtemCaseEvent[] }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading, isError } = useCtemCaseSignals(caseId);
  const groups = useMemo(() => groupOpenedBySignals(data?.signals ?? []), [data]);

  return (
    <Card data-testid="ctem-case-opened-by">
      <CardHeader>
        <CardTitle>{t_i18n("Opened by")}</CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading && !data ? (
          <Skeleton className="h-10 w-full" />
        ) : isError && !data ? (
          <p className="content-base text-default-secondary">{t_i18n("The signals of this case could not be read.")}</p>
        ) : groups.length === 0 ? (
          <p className="content-base text-default-secondary" data-testid="ctem-case-opened-by-origin">
            {t_i18n(NO_SIGNAL_ORIGIN_LABEL[noSignalOrigin(history)])}
          </p>
        ) : (
          <ul className="space-y-3">
            {groups.map((group) => (
              <li key={group.key}>
                {group.signals.length <= OPENED_BY_DETAIL_MAX ? (
                  <ul className="space-y-3">
                    {group.signals.map((s) => (
                      <SignalDetail key={s.id} signal={s} />
                    ))}
                  </ul>
                ) : (
                  <SignalGroup group={group} />
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/** The path label, lens and source of a signal, as chips. */
function PathChips({ signal }: { signal: CtemSignal }) {
  const { t_i18n } = useFormatter();
  return (
    <>
      <span className="flex min-w-0 items-center gap-1.5 font-medium">
        <Icon name="workflow" size={16} className="shrink-0 text-default-secondary" aria-hidden />
        <span className="truncate">{t_i18n(signal.trigger_path_label || signal.trigger_path)}</span>
      </span>
      <ToneBadge tone="neutral">{t_i18n(LENS_LABEL[signal.lens] ?? signal.lens)}</ToneBadge>
      {/* Where the signal came from (CTEM-29-UI): the leak source, OpenCTI for a supplier report. */}
      <ToneBadge tone="neutral" data-testid="ctem-case-opened-by-source">
        {t_i18n("From {source}", { source: signalProviderName(signal.source.provider) })}
      </ToneBadge>
    </>
  );
}

/** One signal in full: a group of one to three keeps today's detail, with the loop trace as a button. */
function SignalDetail({ signal }: { signal: CtemSignal }) {
  const { t_i18n, intl } = useFormatter();
  return (
    <li className="flex min-w-0 flex-col gap-1 content-base" data-testid="ctem-case-opened-by-signal">
      <span className="flex min-w-0 flex-wrap items-center gap-2">
        <PathChips signal={signal} />
        <span className="min-w-0 truncate text-default-secondary" title={signalSubject(signal)}>
          {signalSubject(signal)}
        </span>
        <LoopTraceButton signalId={signal.id} subject={signalSubject(signal)} className="ml-auto" />
      </span>
      <span className="text-default-secondary">{decisionSentence(signal.decision, t_i18n, (iso) => formatDayIntl(intl, iso))}</span>
    </li>
  );
}

/** A group of more than three signals: one compact line, opening into a table. */
function SignalGroup({ group }: { group: OpenedByGroup }) {
  const { t_i18n, intl } = useFormatter();
  const [open, setOpen] = useState(group.signals.length <= OPENED_BY_COLLAPSE_ABOVE);
  const newest = group.signals[0];
  const count = group.signals.length;
  const shown = group.signals.slice(0, SUBJECT_CHIPS);
  const more = count - shown.length;
  const day = (iso: string | null) => (iso ? formatDayIntl(intl, iso) : "");
  const tableId = `ctem-opened-by-group-${newest.id}`;

  return (
    <div className="flex min-w-0 flex-col gap-1.5 content-base" data-testid="ctem-case-opened-by-group">
      <span className="flex min-w-0 flex-wrap items-center gap-2">
        <PathChips signal={newest} />
        <ToneBadge tone="blue" data-testid="ctem-case-opened-by-group-count">
          {t_i18n("{count, plural, one {# signal} other {# signals}}", { count })}
        </ToneBadge>
        <LoopTraceButton signalId={newest.id} subject={signalSubject(newest)} className="ml-auto" />
      </span>
      <span className="text-default-secondary" data-testid="ctem-case-opened-by-group-decision">
        {decisionSentence(newest.decision, t_i18n, (iso) => formatDayIntl(intl, iso))}
      </span>
      {(group.firstSeen || group.lastSeen) && (
        <span className="content-compact text-default-secondary">
          {t_i18n("First seen {first}, last seen {last}", { first: day(group.firstSeen), last: day(group.lastSeen) })}
        </span>
      )}
      <span className="flex min-w-0 flex-wrap items-center gap-1.5" data-testid="ctem-case-opened-by-subjects">
        {shown.map((s) => (
          <ToneBadge key={s.id} tone="neutral" title={signalSubject(s)} className="max-w-[12rem] truncate">
            {signalSubject(s)}
          </ToneBadge>
        ))}
        {more > 0 && (
          <span className="content-compact text-default-secondary" data-testid="ctem-case-opened-by-subjects-more">
            {t_i18n("+{count} more", { count: more })}
          </span>
        )}
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="gap-1"
          aria-expanded={open}
          aria-controls={tableId}
          onClick={() => setOpen((v) => !v)}
          data-testid="ctem-case-opened-by-toggle"
        >
          <Icon name="chevron-down" size={16} className={open ? "rotate-180 transition-transform" : "transition-transform"} aria-hidden />
          <span>{open ? t_i18n("Show less") : t_i18n("Show all {count}", { count })}</span>
        </Button>
      </span>
      {open && <SignalTable id={tableId} signals={group.signals} />}
    </div>
  );
}

/** The signals of a group, one row height each, scrolling past ten rows, filterable above ten. */
function SignalTable({ id, signals }: { id: string; signals: CtemSignal[] }) {
  const { t_i18n, intl } = useFormatter();
  const [filter, setFilter] = useState("");
  const needle = filter.trim().toLowerCase();
  const rows = needle ? signals.filter((s) => signalSubject(s).toLowerCase().includes(needle)) : signals;

  return (
    <div id={id} className="flex min-w-0 flex-col gap-2" data-testid="ctem-case-opened-by-table">
      {signals.length > OPENED_BY_FILTER_ABOVE && (
        <Input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder={t_i18n("Filter the signals")}
          aria-label={t_i18n("Filter the signals")}
          className="max-w-xs"
          data-testid="ctem-case-opened-by-filter"
        />
      )}
      <div className="max-h-[22rem] overflow-y-auto rounded-sm border border-elevation-subtle-soft">
        <table className="w-full table-fixed content-compact">
          <thead className="sticky top-0 bg-elevation-default text-left text-default-secondary">
            <tr>
              <th className="px-2 py-1.5 font-medium">{t_i18n("Subject")}</th>
              <th className="w-28 px-2 py-1.5 font-medium">{t_i18n("First seen")}</th>
              <th className="w-32 px-2 py-1.5 font-medium">{t_i18n("State")}</th>
              <th className="w-36 px-2 py-1.5">
                <span className="sr-only">{t_i18n("Loop trace")}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => (
              <tr key={s.id} className="h-10 border-t border-elevation-subtle-soft" data-testid="ctem-case-opened-by-signal">
                <td className="truncate px-2" title={signalSubject(s)}>
                  {signalSubject(s)}
                </td>
                <td className="truncate px-2 text-default-secondary">
                  {s.first_seen_at ?? s.observed_at ? formatDayIntl(intl, (s.first_seen_at ?? s.observed_at) as string) : ""}
                </td>
                <td className="truncate px-2 text-default-secondary">{t_i18n(SIGNAL_STATE_LABEL[s.state] ?? s.state)}</td>
                <td className="px-2 text-right">
                  <LoopTraceButton signalId={s.id} subject={signalSubject(s)} compact />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && (
          <p className="px-2 py-3 content-compact text-default-secondary">{t_i18n("No signal matches the filter.")}</p>
        )}
      </div>
    </div>
  );
}
