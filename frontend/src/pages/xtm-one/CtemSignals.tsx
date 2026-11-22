// SPDX-License-Identifier: Apache-2.0

/**
 * The Signals page `/ctem/signals` (#1263): the signals of the exposure
 * management loop and the signal sources, as one page with two tabs instead of
 * the two dialogs it replaces. Reached from the command center (the key row
 * counters, the signal sources button, the activity feed), a case's "Opened
 * by" and the Discovery stage; no menu entry.
 *
 * Signals: the state filter carries the API's counts (the key row's), the
 * trigger path filter is read by the server, the table searches, sorts, pages
 * and exports what the server answered. A row opens its loop trace as a
 * section of the page (`?signal=<id>`), never a modal.
 * Sources: the 12 trigger paths by lens (SignalSourcesPanel).
 */
import { useEffect, useMemo, useRef } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { SlideOver } from "@/components/ui/slide-over";
import { TruncatedText } from "@/components/ui/truncated-text";
import {
  CTEM_SIGNAL_STATES,
  useCtemSignalList,
  useCtemSignalPaths,
  type CtemSignal,
  type CtemSignalState,
} from "@/hooks/api/useCtemSignalsApi";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { CtemPageHeader } from "./ctem/CtemPageHeader";
import { DecisionReason, decisionSentence, SIGNAL_STATE_LABEL } from "./ctem/loop/DecisionReason";
import { SignalSourcesLink, SignalSourcesPanel } from "./ctem/loop/SignalSourcesPanel";
import { SignalTraceBody, signalProviderName } from "./ctem/loop/SignalLoopTrace";
import { connectedCount } from "./ctem/loop/signal-sources";
import { readSignalsView, SIGNAL_SOURCES_PATH, SIGNALS_PARAMS, SIGNALS_PATH, SIGNALS_TABLE_URL_KEY } from "./ctem/loop/signals-route";

type Tab = "signals" | "sources";
type StateFilter = CtemSignalState | "all";

/** The page size the table reads from the API (its `MAX_PAGE`), every page loaded. */
const API_PAGE = 200;

/**
 * Column widths of the fixed layout (UX r1 B2 on #1272): the six columns fit the table's
 * 844 px at a 1080 px window, en and fr, without a sideways scroll; "Signal" takes what is
 * left. "Case" steps aside under the 6xl container width (back from the Columns pill).
 */
const COLUMN_WIDTH = { path: 160, source: 130, state: 220, firstSeen: 140, case: 104 } as const;

export default function CtemSignals() {
  const { t_i18n } = useFormatter();
  const { count } = useNumberFormat();
  const { tab: tabParam } = useParams();
  const tab: Tab = tabParam === "sources" ? "sources" : "signals";
  const { data: pathData } = useCtemSignalPaths();
  const paths = pathData?.paths ?? [];
  return (
    <div className="space-y-6" data-testid="ctem-signals-page">
      <CtemPageHeader
        back={{ to: "/", label: "Back to Command Center" }}
        crumb={t_i18n("Signals")}
        title={t_i18n("Signals")}
        hint={t_i18n("The changes in exposure that start the exposure management loop, where they come from, and what the loop decided for each.")}
        hintLabel={t_i18n("About signals")}
      />
      <DetailTabs<Tab>
        tabs={[
          { key: "signals", label: "Signals", glyph: "activity", to: SIGNALS_PATH, testId: "ctem-signals-tab-signals" },
          {
            key: "sources",
            label: "Sources",
            glyph: "satellite-dish",
            to: SIGNAL_SOURCES_PATH,
            testId: "ctem-signals-tab-sources",
            trailing:
              paths.length > 0 ? (
                <span className="tabular-nums text-default-secondary">
                  {count(connectedCount(paths))}/{count(paths.length)}
                </span>
              ) : undefined,
          },
        ]}
        active={tab}
        ariaLabel={t_i18n("Signals")}
        testId="ctem-signals-tabs"
      />
      {tab === "sources" ? (
        <section className="flex flex-col gap-4" aria-label={t_i18n("Signal sources")}>
          {paths.length > 0 && (
            <p className="content-compact text-default-secondary" data-testid="ctem-signal-sources-summary">
              {t_i18n("{connected} of {total} trigger paths connected. Every signal starts the exposure management loop.", {
                connected: count(connectedCount(paths)),
                total: count(paths.length),
              })}
            </p>
          )}
          <SignalSourcesPanel />
        </section>
      ) : (
        <SignalsTab />
      )}
    </div>
  );
}

function SignalsTab() {
  const { t_i18n } = useFormatter();
  const [params, setParams] = useSearchParams();
  const view = readSignalsView(params);
  const { data: pathData } = useCtemSignalPaths();
  const paths = pathData?.paths ?? [];
  // The state is a server filter (its counts are the key row's); the trigger path and the source are
  // the table's own filters (several values each), on every signal of the state, read page by page.
  const { data, isLoading, isError, refetch, hasNextPage, fetchNextPage, isFetchingNextPage } = useCtemSignalList(view.state, API_PAGE);
  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage) void fetchNextPage();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);
  const first = data?.pages[0];
  const counts = first?.counts;
  const all = counts ? CTEM_SIGNAL_STATES.reduce((sum, s) => sum + (counts[s] ?? 0), 0) : undefined;
  const signals = useMemo(() => data?.pages.flatMap((page) => page.signals) ?? [], [data]);
  const anyConnected = paths.some((p) => p.status === "connected");

  const setParam = (name: string, value: string | null) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set(name, value);
        else next.delete(name);
        return next;
      },
      { replace: name !== SIGNALS_PARAMS.signal },
    );

  const columns: DataTableColumn<CtemSignal>[] = [
    {
      key: "subject",
      header: "Signal",
      cell: (s) => (
        <span className="block min-w-0" data-signal-open={s.id === view.signal ? "" : undefined}>
          <TruncatedText text={s.subject.label || s.subject.ref} className="font-medium" />
        </span>
      ),
      sortValue: (s) => (s.subject.label || s.subject.ref).toLowerCase(),
      csv: (s) => s.subject.label || s.subject.ref,
    },
    {
      key: "path",
      header: "Trigger path",
      hideable: true,
      width: COLUMN_WIDTH.path,
      cell: (s) => <TruncatedText text={t_i18n(s.trigger_path_label || s.trigger_path)} />,
      sortValue: (s) => s.trigger_path_label || s.trigger_path,
      csv: (s) => s.trigger_path,
    },
    {
      key: "source",
      header: "Source",
      hideable: true,
      width: COLUMN_WIDTH.source,
      cell: (s) => (
        <span className="flex min-w-0 items-center gap-2" data-testid="ctem-signal-source-cell">
          <ProviderLogo provider={s.source.provider} className="size-4 shrink-0" decorative />
          <TruncatedText text={signalProviderName(s.source.provider)} />
        </span>
      ),
      sortValue: (s) => signalProviderName(s.source.provider),
      csv: (s) => s.source.provider,
    },
    {
      // The state and why (a discarded, watchlisted or opened signal always says why).
      key: "state",
      header: "State and why",
      width: COLUMN_WIDTH.state,
      cell: (s) => <DecisionReason state={s.state} decision={s.decision} />,
      sortValue: (s) => CTEM_SIGNAL_STATES.indexOf(s.state),
      csv: (s) => `${s.state}: ${decisionSentence(s.decision, t_i18n, (iso) => iso.slice(0, 10))}`,
    },
    {
      key: "first_seen",
      header: "First seen",
      hideable: true,
      width: COLUMN_WIDTH.firstSeen,
      className: "whitespace-nowrap",
      cell: (s) => <DateTimeCell value={s.first_seen_at} />,
      sortValue: (s) => s.first_seen_at ?? "",
      csv: (s) => s.first_seen_at ?? "",
    },
    {
      // Plain text (the whole row is the target, gate 10): the loop trace the row opens links the case.
      key: "case",
      header: "Case",
      hideable: true,
      hideBelow: "6xl",
      width: COLUMN_WIDTH.case,
      className: "whitespace-nowrap",
      cell: (s) => <span className={s.case_id ? undefined : "text-default-secondary"}>{s.case_id ? t_i18n("Case opened") : t_i18n("None")}</span>,
      sortValue: (s) => (s.case_id ? 1 : 0),
      csv: (s) => s.case_id ?? "",
    },
  ];

  const sources = Array.from(new Set(signals.map((s) => s.source.provider)));

  // The row whose trace is open stays in view in the table's own scroller (UX r1 B1 on #1272).
  const tableBox = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!view.signal) return;
    const frame = requestAnimationFrame(() => {
      const row = tableBox.current?.querySelector<HTMLElement>("[data-signal-open]")?.closest("tr");
      row?.scrollIntoView?.({ block: "nearest" });
    });
    return () => cancelAnimationFrame(frame);
  }, [view.signal, signals.length]);

  return (
    <section ref={tableBox} className="flex flex-col gap-4" aria-label={t_i18n("Signals")}>
      <SegmentedFilter<StateFilter>
        selection="radio"
        overflow="menu"
        ariaLabel={t_i18n("Filter by state")}
        value={view.state ?? "all"}
        onChange={(next) => setParam(SIGNALS_PARAMS.state, next === "all" ? null : next)}
        options={[
          { key: "all", label: "All", count: all, countPending: all === undefined },
          ...CTEM_SIGNAL_STATES.map((s) => ({
            key: s as StateFilter,
            label: SIGNAL_STATE_LABEL[s],
            count: counts?.[s],
            countPending: !counts,
          })),
        ]}
      />
      {/* The trace opens in the side sheet, out of the page flow: the table stays the page's last
          block, keeps its fit-to-viewport scroller and the open row in view, and the page never
          scrolls (UX r2 B1 on #1272; an inline card above the table dropped it out of its fit mode). */}
      <SlideOver
        open={!!view.signal}
        onClose={() => setParam(SIGNALS_PARAMS.signal, null)}
        label={t_i18n("Loop trace")}
        width="w-[min(560px,45vw)]"
      >
        <div className="flex h-full min-h-0 flex-col" data-testid="ctem-loop-trace">
          <div className="flex items-start justify-between gap-3 border-b border-elevation-subtle p-4">
            <div className="flex min-w-0 flex-col gap-1">
              <h2 className="title-sm">{t_i18n("Loop trace")}</h2>
              <p className="content-compact text-default-secondary">{t_i18n("Where this signal is in the exposure management loop, and why.")}</p>
            </div>
            <Button variant="ghost" size="sm" onClick={() => setParam(SIGNALS_PARAMS.signal, null)} data-testid="ctem-loop-trace-close">
              <Icon name="x" size={16} aria-hidden />
              {t_i18n("Close")}
            </Button>
          </div>
          {/* The sheet's only scroller: the trace body. */}
          <div className="min-h-0 flex-1 overflow-y-auto p-4" data-testid="ctem-loop-trace-scroll">
            {view.signal && <SignalTraceBody id={view.signal} />}
          </div>
        </div>
      </SlideOver>
      <DataTable
        rows={signals}
        layout="fixed"
        columns={columns}
        rowKey={(s) => s.id}
        onRowClick={(s) => setParam(SIGNALS_PARAMS.signal, s.id)}
        rowClassName={(s) => (s.id === view.signal ? "bg-elevation-hover" : undefined)}
        defaultSort={{ key: "first_seen", direction: "desc" }}
        defaultPageSize={25}
        rowLines={2}
        searchFields={(s) => [s.subject.label, s.subject.ref, s.trigger_path_label, signalProviderName(s.source.provider)]}
        searchPlaceholder={t_i18n("Search signal, path or source")}
        filtersInPill
        filters={[
          {
            key: "path",
            label: "Trigger path",
            values: (s) => [s.trigger_path],
            options: paths.map((p) => ({ value: p.id, label: t_i18n(p.label) })),
          },
          {
            key: "source",
            label: "Source",
            values: (s) => [s.source.provider],
            options: sources.map((p) => ({ value: p, label: signalProviderName(p) })),
          },
        ]}
        exportName="ctem-signals"
        urlKey={SIGNALS_TABLE_URL_KEY}
        storageKey="ctem-signals"
        isLoading={isLoading && !data}
        isError={isError}
        onRetry={() => void refetch()}
        emptyLabel={anyConnected ? t_i18n("No signal seen yet: your sources are connected and nothing reached the loop.") : t_i18n("No signal yet: no source is connected.")}
        emptyContent={
          <div className="flex flex-col items-start gap-2" data-testid="ctem-signal-empty">
            <p className="content-compact text-default-secondary">
              {anyConnected
                ? t_i18n("No signal seen yet: your sources are connected and nothing reached the loop.")
                : t_i18n("No signal yet: no source is connected.")}
            </p>
            <SignalSourcesLink />
          </div>
        }
        noResultsLabel={t_i18n("No signal matches these filters")}
        rowTestId="ctem-signal-row"
      />
    </section>
  );
}
