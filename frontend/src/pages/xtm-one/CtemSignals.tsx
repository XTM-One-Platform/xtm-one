// SPDX-License-Identifier: Apache-2.0

/**
 * The Signals page `/ctem/signals` (#1263): the signals of the exposure
 * management loop. A row of the CTEM menu group between Cases and Inventory
 * (Sam, 23 Nov 2026, #1401), titled with the app's standard list header like
 * Cases, no back link; also reached from the command center (the key row
 * counters, the signal sources button, the activity feed), a case's
 * "Opened by" and the Discovery stage.
 *
 * No tabs (#1519, the Inventory Sources pattern of #1476 and Platform Health's
 * settings button): the header carries a secondary "Sources" button top right
 * with the connected count, which opens the signal sources as a page of their
 * own, `/ctem/signals/sources` (SignalSourcesPanel), with "Back to Signals" first.
 *
 * Signals: the state filter carries the API's counts (the key row's), the
 * trigger path filter is read by the server, the table searches, sorts, pages
 * and exports what the server answered. A row opens its loop trace as a
 * section of the page (`?signal=<id>`), never a modal.
 *
 * Above the list, the summary strip (SignalsSummaryStrip): signals in 30 days,
 * unmatched, the signals with a case and the trigger paths connected with the sources'
 * logos, so the page never reads empty (Sam, 22 Nov 2026).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { DataTable, TOOLBAR_CONTROL_HEIGHT, type DataTableColumn } from "@/components/ui/data-table";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { useToast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { BackButton } from "@/components/ui/back-button";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { SourceBadge } from "@/components/ui/source-badge";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useAuth } from "@/hooks/useAuth";
import { useCompactToolbar } from "@/hooks/useCompactToolbar";
import {
  CTEM_SIGNAL_STATES,
  useAddCtemSignalsToInventory,
  useCtemSignalList,
  useTriageCtemSignals,
  type CtemSignalTriage,
  type CtemSignalTriageAction,
  useCtemSignalPaths,
  type CtemSignal,
  type CtemSignalState,
} from "@/hooks/api/useCtemSignalsApi";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { DecisionReason, decisionSentence, SIGNAL_STATE_LABEL } from "./ctem/loop/DecisionReason";
import { SignalSourcesPanel } from "./ctem/loop/SignalSourcesPanel";
import { signalProviderName } from "./ctem/loop/SignalLoopTrace";
import { LoopTraceSheet } from "./ctem/loop/LoopTraceSheet";
import { SignalsSummaryStrip } from "./ctem/loop/SignalsSummaryStrip";
import { connectedCount } from "./ctem/loop/signal-sources";
import { readSignalsView, SIGNAL_SOURCES_PATH, SIGNALS_PARAMS, SIGNALS_PATH, SIGNALS_TABLE_URL_KEY, selectAllScope } from "./ctem/loop/signals-route";

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
  const { tab } = useParams();
  // The signal sources are a page of their own (#1519), the Inventory Sources pattern (#1476): any other
  // segment under /ctem/signals reads as the list.
  return tab === "sources" ? <SignalSourcesPage /> : <SignalsListPage />;
}

/**
 * The signals list (#1519: no tabs): the standard list header with Sources as its secondary action top
 * right (like Platform Health's settings and the Inventory Sources button), the summary strip, the list.
 */
function SignalsListPage() {
  const { t_i18n } = useFormatter();
  const { count } = useNumberFormat();
  const { data: pathData } = useCtemSignalPaths();
  const paths = pathData?.paths ?? [];
  return (
    <div className="space-y-6" data-testid="ctem-signals-page">
      <ListPageHeader
        title={t_i18n("Signals")}
        info={t_i18n("The changes in exposure that start the exposure management loop, where they come from, and what the loop decided for each.")}
        infoLabel={t_i18n("About signals")}
        actions={
          // Same audience as the page: a reader sees the sources too, each Connect is gated in the panel.
          <Button variant="secondary" className="gap-1.5" asChild>
            <Link to={SIGNAL_SOURCES_PATH} data-testid="ctem-signals-sources">
              <Icon name="satellite-dish" size={16} aria-hidden /> {t_i18n("Sources")}
              {paths.length > 0 && (
                <span className="tabular-nums text-default-secondary" data-testid="ctem-signals-sources-count">
                  {count(connectedCount(paths))}/{count(paths.length)}
                </span>
              )}
            </Link>
          </Button>
        }
      />
      <SignalsSummaryStrip />
      <SignalsTab />
    </div>
  );
}

/** The signal sources page (#1519): the standard back link to the signals first, its title, the sources table. */
function SignalSourcesPage() {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-4" data-testid="ctem-signal-sources-page">
      <BackButton to={SIGNALS_PATH} label="Back to Signals" />
      <ListPageHeader
        title={t_i18n("Signal sources")}
        info={t_i18n("The trigger paths that start the exposure management loop, by lens, with the sources that feed each one on this platform and whether they are connected.")}
        infoLabel={t_i18n("About the signal sources")}
      />
      <SignalSourcesPanel />
    </div>
  );
}

/**
 * The type of a bulk action button: the toolbar's compact 12 px / 500, like Filters and Columns.
 * `content-compact` alone loses to the design system Button's own decomposed
 * `text-content-button font-semibold` (14 px / 600), so the decomposed compact utilities
 * replace them through tailwind-merge (UX round 2 on #1521, B1).
 */
const BULK_BUTTON_TYPE = "px-2 font-content-compact text-content-compact leading-content-compact tracking-content-compact font-medium";

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
  const anyConnected = connectedCount(paths) > 0;

  // Triage (#1505): the selected rows, or every signal of the state filter, dismissed,
  // restored or checked again in one call. A signal that opened a case cannot be selected.
  const { toast } = useToast();
  const triage = useTriageCtemSignals();
  const addToInventory = useAddCtemSignalsToInventory();
  const { isAdmin } = useAuth();
  const busy = triage.isPending || addToInventory.isPending;
  const [selected, setSelected] = useState<string[]>([]);
  const [allMatching, setAllMatching] = useState(false);
  // "Select all" covers what the table shows (review on #1521): the state and at most one trigger
  // path, both read by the server; a search or a source filter offers no "select all".
  const scope = selectAllScope(params);
  const scopeKey = JSON.stringify(scope);
  const stateTotal =
    view.state && counts && scope
      ? scope.trigger_path
        ? signals.filter((s) => s.trigger_path === scope.trigger_path).length
        : (counts[view.state] ?? 0)
      : 0;
  const matchingFilter = view.state && scope ? { state: view.state, ...scope } : null;
  useEffect(() => {
    setSelected([]);
    setAllMatching(false);
  }, [view.state, scopeKey]);
  const selectedRows = signals.filter((s) => selected.includes(s.id));
  const anyDismissed = selectedRows.some((s) => s.decision?.reason_code === "dismissed_by_user");
  const anyOpen = allMatching ? view.state !== "discarded" : selectedRows.some((s) => s.decision?.reason_code !== "dismissed_by_user");
  const runTriage = (action: CtemSignalTriageAction) => {
    const body: CtemSignalTriage = allMatching && matchingFilter ? { action, filter: matchingFilter } : { action, ids: selected };
    triage.mutate(body, {
      onSuccess: (result) => {
        setSelected([]);
        setAllMatching(false);
        const count = action === "recheck" ? result.matched - (result.counts.skipped ?? 0) : (result.counts.changed ?? 0);
        // Nothing changed is never a success (UX round 1 on #1521, B2): it says so, neutral.
        if (count <= 0) {
          toast({ title: t_i18n("No signal changed"), variant: "info" });
          return;
        }
        const title =
          action === "dismiss"
            ? t_i18n("{count, plural, one {# signal dismissed} other {# signals dismissed}}", { count })
            : action === "restore"
              ? t_i18n("{count, plural, one {# signal restored} other {# signals restored}}", { count })
              : t_i18n("{count, plural, one {# signal checked again} other {# signals checked again}}", { count });
        toast({ title, variant: "success" });
      },
      onError: () => toast({ title: t_i18n("The signals could not be updated"), variant: "error" }),
    });
  };
  // Add to inventory (#1505, administrators): only an unmatched signal has a subject to declare.
  const compactToolbar = useCompactToolbar();
  const anyUnmatched = allMatching ? view.state === "to_validate" : selectedRows.some((s) => s.state === "to_validate");
  const runAddToInventory = () => {
    const body = allMatching && matchingFilter ? { filter: matchingFilter } : { ids: selected };
    addToInventory.mutate(body, {
      onSuccess: (result) => {
        setSelected([]);
        setAllMatching(false);
        const added = result.added.assets.length + result.added.products.length + result.added.domains.length;
        toast({
          title: t_i18n("{count, plural, one {# entry added to the inventory} other {# entries added to the inventory}}", { count: added }),
          variant: "success",
        });
      },
      onError: () => toast({ title: t_i18n("The signals could not be updated"), variant: "error" }),
    });
  };
  const canSelectAllMatching = !!matchingFilter && !allMatching && selected.length > 0 && stateTotal > selected.length;
  // The bulk actions the selection allows, most frequent first. Check again is offered only
  // for a selection that holds a signal not dismissed: a dismissed one stays dismissed on a
  // re-check (UX round 1 on #1521, B2). Two show as toolbar buttons (one below 1440 px), the
  // rest in the shared "More actions" menu, so the toolbar keeps one 32 px row at 1080 with a
  // selection (UX rounds 1 and 2 on #1521, B1).
  const bulkActions: { id: string; label: string; glyph: "plus" | "eye-off" | "refresh-cw" | "rotate-ccw"; run: () => void }[] = [
    ...(isAdmin && anyUnmatched ? [{ id: "add-to-inventory", label: t_i18n("Add to inventory"), glyph: "plus" as const, run: runAddToInventory }] : []),
    ...(anyOpen ? [{ id: "dismiss", label: t_i18n("Dismiss signals"), glyph: "eye-off" as const, run: () => runTriage("dismiss") }] : []),
    ...(anyOpen ? [{ id: "recheck", label: t_i18n("Check again"), glyph: "refresh-cw" as const, run: () => runTriage("recheck") }] : []),
    ...(anyDismissed || (allMatching && view.state === "discarded")
      ? [{ id: "restore", label: t_i18n("Restore"), glyph: "rotate-ccw" as const, run: () => runTriage("restore") }]
      : []),
  ];
  // Below 1440 px the "select all N" link takes the one inline slot when a menu follows, so the
  // bar still fits one 32 px row at 1080 with a trigger path filter and a selection (UX round 4 on
  // #1521, B1). A lone action stays a button: a menu of one row hides it for nothing.
  const inlineCount = compactToolbar ? (canSelectAllMatching && bulkActions.length > 1 ? 0 : 1) : 2;
  const inlineActions = bulkActions.slice(0, inlineCount);
  const menuActions: OverflowMenuItem[] = bulkActions.slice(inlineCount).map((a) => ({ id: a.id, label: a.label, glyph: a.glyph, onSelect: a.run, disabled: busy }));
  const selectionActions = (
    <span className="flex flex-wrap items-center gap-2" data-testid="ctem-signals-bulk-actions">
      {canSelectAllMatching && (
        <Button variant="link" size="sm" className={cn(TOOLBAR_CONTROL_HEIGHT, BULK_BUTTON_TYPE)} onClick={() => setAllMatching(true)}>
          {t_i18n("select all {count}", { count: stateTotal })}
        </Button>
      )}
      {inlineActions.map((a) => (
        <Button
          key={a.id}
          variant="outline"
          size="sm"
          className={cn(TOOLBAR_CONTROL_HEIGHT, BULK_BUTTON_TYPE)}
          disabled={busy}
          onClick={a.run}
          data-testid={`ctem-signals-${a.id}`}
        >
          <Icon name={a.glyph} size={16} aria-hidden />
          {a.label}
        </Button>
      ))}
      {menuActions.length > 0 && <OverflowMenu label={t_i18n("More actions")} items={menuActions} busy={busy} />}
    </span>
  );

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
        // The shared source badge (#1357): the official emblem when one exists, then the name.
        <SourceBadge source={s.source.provider} label={signalProviderName(s.source.provider)} className="max-w-full" testId="ctem-signal-source-cell" />
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
      <LoopTraceSheet signalId={view.signal} onClose={() => setParam(SIGNALS_PARAMS.signal, null)} />
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
        // The table's standard empty statement alone (Sam, 23 Nov 2026, #1344): the summary strip
        // above carries the connected count and the Sources page is one click away (header button).
        emptyLabel={anyConnected ? t_i18n("No signal seen yet: your sources are connected and nothing reached the loop.") : t_i18n("No signal yet: no source is connected.")}
        noResultsLabel={t_i18n("No signal matches these filters")}
        rowTestId="ctem-signal-row"
        selection={{
          selected,
          onChange: (keys) => {
            setAllMatching(false);
            setSelected(keys);
          },
          count: allMatching ? stateTotal : undefined,
          actions: selectionActions,
          disabledReason: (key) =>
            signals.find((s) => s.id === key)?.state === "case_opened" ? t_i18n("This signal opened a case: the case owns it now") : undefined,
        }}
      />
    </section>
  );
}
