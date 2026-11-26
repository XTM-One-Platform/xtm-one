// SPDX-License-Identifier: Apache-2.0

/**
 * The signal sources (#1189, dev-docs/ctem-signals.md section 2bis):
 * every trigger path the loop knows, read from `GET /ctem-signals/paths` and
 * never hard-coded, as a compact table grouped by lens (Sam, 23 Nov 2026, #1344):
 * trigger path (label, description on line 2), sources (logo and name, the kind
 * as secondary text, the event that fires it in a tooltip; only what feeds the
 * path on this platform, a provider with no integration here folded into one
 * muted "+N possible", Sam 23 Nov 2026, #1401), status (its own
 * column so every chip aligns: Connected, Off, Not connected, or Agents and API
 * only, a path with no automatic source never reading connected), signals in 30
 * days, last signal, and one action: Connect or Turn on the first unbound
 * source, or Open signals in the signals list. Rows of at most two lines; below
 * the 4xl container width the Sources and Last signal columns step into line 2
 * of the first cell.
 *
 * The signal sources page (`/ctem/signals/sources`, #1263, its own page since #1519),
 * reached from the Signals page's header button and its summary strip.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Chip, Icon, type IconName } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { useTriggerTabIndex } from "@/components/ui/row-target-context";
import { Tip } from "@/components/ui/tooltip";
import {
  type CtemPlatformSource,
  type CtemSignalPath,
  type CtemSignalPathSource,
  useCtemPlatformSources,
  useCtemSignalPaths,
} from "@/hooks/api/useCtemSignalsApi";
import { useAuth } from "@/hooks/useAuth";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { formatShortDateTimeIntl } from "@/lib/relative-time";
import { CTEM_PRODUCT_INFO, INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { SIGNAL_STATE_LABEL } from "./DecisionReason";
import { PlatformSourceEnableDialog } from "./PlatformSourceControls";
import { SignalPathSourcesSheet } from "./SignalPathSourcesSheet";
import { isProvisioned } from "./platform-sources";
import { signalsHref } from "./signals-route";
import {
  connectHref,
  connectNeedsAdmin,
  connectTarget,
  LENS_ICON,
  PATH_ICON,
  PATH_STATUS_LABEL,
  PATH_STATUS_ORDER,
  PATH_STATUS_TONE,
  pathStatus,
  pathStatusLabel,
  SOURCE_KIND_LABEL,
  connectedProviders,
  sourceKindText,
  sourceState,
  sourceStateIcon,
  splitSources,
} from "./signal-sources";

const LENSES = ["inside", "outside", "world"] as const;

/** The state word after a source in the folded line; bound and not connected say none. @i18n-keys */
const COMPACT_STATE_WORD = {
  error: "Error",
  off: "Off",
  not_set_up: "Not set up",
  excluded: "Not used here",
} as const satisfies Partial<Record<ReturnType<typeof sourceState>, string>>;

/** The lens headings and what each sees. @i18n-keys */
const LENS_HEADING = { inside: "Inside", outside: "Outside", world: "World" } as const;
/** @i18n-keys */
const LENS_HELP = {
  inside: "What your own tools see in your environment",
  outside: "What an attacker sees of you from the internet",
  world: "What happens in the threat landscape that may concern you",
} as const;

/**
 * One source: logo and name, the kind as secondary text, the event that fires it in a tooltip (never a third line).
 * A pass turned off says Off after its kind; a source of a connected provider that is not set up says Not set up
 * (UX round 1 on #1405, B1); a source whose provider is not connected here (shown only on a path with nothing live)
 * draws a greyed logo and a muted name, and its tooltip says so.
 */
function SourceItem({ source, connected }: { source: CtemSignalPathSource; connected: ReadonlySet<string> }) {
  const { t_i18n } = useFormatter();
  // The loop trace's naming (signalProviderName), read here without importing that module: it imports this one.
  const name = sourceName(source);
  // The row opens the path's sources (#1445): it is the one tab stop, the tooltip triggers add none.
  const tabIndex = useTriggerTabIndex();
  const state = sourceState(source, connected);
  const possible = state === "not_connected";
  const event = source.event ? t_i18n(source.event) : null;
  const icon = sourceStateIcon(state);
  const stateLine = possible ? t_i18n("Not connected on this platform") : icon ? t_i18n(icon.label) : null;
  const tip = [event, stateLine].filter(Boolean).join(". ") || null;
  return (
    <Tip content={tip}>
      <span
        tabIndex={tabIndex}
        className="inline-flex min-w-0 items-center gap-1.5 rounded-sm"
        data-testid="ctem-signal-source"
        data-provider={source.provider}
        data-kind={source.kind}
        data-bound={source.bound}
        data-off={source.off ? true : undefined}
        data-possible={possible ? true : undefined}
        data-state={state}
      >
        {icon && <Icon name={icon.icon} size={16} className={cn("shrink-0", icon.className)} aria-hidden data-testid="ctem-signal-source-status-icon" />}
        <ProviderLogo provider={source.provider} className={cn("size-4 shrink-0", possible && "opacity-50 grayscale")} decorative />
        <span className={source.bound ? "truncate" : "truncate text-default-secondary"}>{name}</span>
        <span className="shrink-0 content-caption text-default-secondary">{sourceKindText(t_i18n, source)}</span>
        {source.off && <span className="shrink-0 content-caption text-default-secondary">{t_i18n("Off")}</span>}
        {state === "excluded" && <span className="shrink-0 content-caption text-default-secondary">{t_i18n("Not used here")}</span>}
        {state === "not_set_up" && <span className="shrink-0 content-caption text-default-secondary">{t_i18n("Not set up")}</span>}
        {state === "error" && <span className="shrink-0 content-caption text-feedback-error-primary">{t_i18n("Error")}</span>}
      </span>
    </Tip>
  );
}

/** The sources a path could have but this platform has no integration for: one muted item, the list in its tooltip. */
function PossibleSources({ sources }: { sources: CtemSignalPathSource[] }) {
  const { t_i18n } = useFormatter();
  const tabIndex = useTriggerTabIndex();
  const tip = (
    <span className="flex flex-col gap-0.5">
      {sources.map((s) => (
        <span key={s.key ?? `${s.provider}:${s.kind}`}>{`${sourceName(s)} (${sourceKindText(t_i18n, s)})`}</span>
      ))}
      <span className="text-default-secondary">{t_i18n("Not connected on this platform")}</span>
    </span>
  );
  return (
    <Tip content={tip}>
      <span
        tabIndex={tabIndex}
        className="inline-flex shrink-0 items-center rounded-sm content-caption text-default-secondary"
        data-testid="ctem-signal-sources-possible"
        data-providers={sources.map((s) => s.provider).join(" ")}
      >
        {t_i18n("{count, plural, other {+# possible}}", { count: sources.length })}
      </span>
    </Tip>
  );
}

/** The sources not used on this path: one muted item, the list in its tooltip, like "+N possible" (UX round 1 on #2454, B1). */
function UnusedSources({ sources }: { sources: CtemSignalPathSource[] }) {
  const { t_i18n } = useFormatter();
  const tabIndex = useTriggerTabIndex();
  const tip = (
    <span className="flex flex-col gap-0.5">
      {sources.map((s) => (
        <span key={s.key ?? `${s.provider}:${s.kind}`}>{`${sourceName(s)} (${sourceKindText(t_i18n, s)})`}</span>
      ))}
      <span className="text-default-secondary">{t_i18n("Not used here")}</span>
    </span>
  );
  return (
    <Tip content={tip}>
      <span
        tabIndex={tabIndex}
        className="inline-flex shrink-0 items-center rounded-sm content-caption text-default-secondary"
        data-testid="ctem-signal-sources-unused"
      >
        {t_i18n("{count, plural, other {+# not used here}}", { count: sources.length })}
      </span>
    </Tip>
  );
}

/** The 30-day count with its per-state tooltip; no tab stop of its own inside a row that opens (#1445). */
function CountCell({ tip, title, label }: { tip: string; title: string; label: string }) {
  const tabIndex = useTriggerTabIndex();
  return (
    <Tip content={tip}>
      <span className="tabular-nums" tabIndex={tabIndex} title={title} data-testid="ctem-signal-sources-30d">
        {label}
      </span>
    </Tip>
  );
}

function sourceName(source: CtemSignalPathSource): string {
  return CTEM_PRODUCT_INFO[source.provider]?.name ?? INTEGRATION_PROVIDER_NAMES[source.provider] ?? source.provider;
}

/**
 * The sources of a path. `compact` (line 2 of a folded row) keeps one line: the names
 * cut with an ellipsis, the whole list in the tooltip, never a wrap to a third line
 * (UX round 1 on #1356, B2).
 */
function SourceList({
  path,
  connected,
  className,
  compact,
}: {
  path: CtemSignalPath;
  connected: ReadonlySet<string>;
  className?: string;
  compact?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const tabIndex = useTriggerTabIndex();
  const sources = path.sources ?? [];
  if (sources.length === 0) {
    return <span className={className ? `${className} text-default-secondary` : "text-default-secondary"}>{t_i18n("None")}</span>;
  }
  // Nothing live: the path reads Not connected, so its possible sources are listed muted (Connect names the first).
  // Something live: only the live sources are listed, the possible ones folded into "+N possible" (#1401).
  // Live is every source of a connected provider, a source not set up included: it says Not set up, never
  // "Not connected on this platform" (UX round 1 on #1405, B1).
  const { live, possible } = splitSources(path, connected);
  const shown = live.length > 0 ? live : possible;
  const folded = live.length > 0 ? possible : [];
  if (compact) {
    const stateWord = (s: CtemSignalPathSource) => {
      const state = sourceState(s, connected);
      const word = state in COMPACT_STATE_WORD ? COMPACT_STATE_WORD[state as keyof typeof COMPACT_STATE_WORD] : null;
      return word ? `, ${t_i18n(word)}` : "";
    };
    const label = (s: CtemSignalPathSource) => `${sourceName(s)} (${sourceKindText(t_i18n, s)}${stateWord(s)})`;
    const names = shown.map(label).join(", ");
    const tip = [
      names,
      folded.length > 0 ? `${t_i18n("Not connected on this platform")}: ${folded.map(label).join(", ")}` : null,
    ].filter(Boolean).join(". ");
    return (
      <Tip content={tip}>
        <span tabIndex={tabIndex} className="flex min-w-0 flex-1 items-center gap-2 rounded-sm" data-testid="ctem-signal-sources-compact">
          <span className={cn("min-w-0 truncate", !live.some((s) => s.bound) && "text-default-secondary")}>{names}</span>
          {folded.length > 0 && (
            <span className="shrink-0" data-testid="ctem-signal-sources-compact-possible">
              {t_i18n("{count, plural, other {+# possible}}", { count: folded.length })}
            </span>
          )}
        </span>
      </Tip>
    );
  }
  // A source not used on this path folds into one muted "+N not used here" while something else is listed, so the
  // cell keeps to two lines (UX round 1 on #2454, B1: the default-off named sources wrapped a row to a third line).
  const notUsed = (s: CtemSignalPathSource) => sourceState(s, connected) === "excluded";
  const unused = shown.some((s) => !notUsed(s)) ? shown.filter(notUsed) : [];
  const listed = unused.length > 0 ? shown.filter((s) => !notUsed(s)) : shown;
  return (
    <span className={className ? `${className} flex min-w-0 flex-wrap items-center gap-x-3` : "flex min-w-0 flex-wrap items-center gap-x-3"}>
      {listed.map((s) => (
        <SourceItem key={s.key ?? `${s.provider}:${s.kind}`} source={s} connected={connected} />
      ))}
      {unused.length > 0 && <UnusedSources sources={unused} />}
      {folded.length > 0 && <PossibleSources sources={folded} />}
    </span>
  );
}

/**
 * The signal sources page (#1263, #1519): every trigger path by lens,
 * its status, sources, signals in 30 days and Connect. A path's "Open signals"
 * opens the signals list filtered on it; a row opens its sources in the side
 * panel, where an administrator manages them (#1445).
 */
export function SignalSourcesPanel() {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const { count, exactCount } = useNumberFormat();
  const { isAdmin } = useAuth();
  const { data, isLoading, isError, refetch } = useCtemSignalPaths();
  const paths = data?.paths ?? [];
  // The path whose sources the side panel shows (#1445): read from the live list so a save refreshes it.
  const [openPath, setOpenPath] = useState<string | null>(null);
  const opened = paths.find((p) => p.id === openPath) ?? null;
  // The platform sources an administrator turns on from the row's Set up (#2434).
  const { data: platform } = useCtemPlatformSources(isAdmin);
  const platformSources = platform?.sources ?? [];
  const [enabling, setEnabling] = useState<CtemPlatformSource | null>(null);
  const firstOff = (source: CtemSignalPathSource) =>
    platformSources.find((s) => s.provider === source.provider && s.kind === source.kind && !s.enabled && s.integration_connected) ?? null;
  // Read once over every path: a provider connected on one path is connected on all of them.
  // A provider whose source XTM One provisions is connected once its integration is, even with every
  // source still off (#2434): its sources read Not set up, never Not connected.
  const connected = new Set([...connectedProviders(paths), ...platformSources.filter((s) => s.integration_connected).map((s) => s.provider)]);
  const lastSignal = (path: CtemSignalPath) =>
    path.last_signal_at ? formatShortDateTimeIntl(intl, path.last_signal_at) : t_i18n("None");

  const columns: DataTableColumn<CtemSignalPath>[] = [
    {
      key: "path",
      header: "Trigger path",
      sortValue: (path) => t_i18n(path.label),
      cell: (path) => (
        <span className="flex min-w-0 items-center gap-2.5">
          <span className="flex size-7 shrink-0 items-center justify-center rounded-sm bg-elevation-hover text-accent" aria-hidden>
            <Icon name={(PATH_ICON[path.id] ?? "activity") as IconName} size={16} />
          </span>
          <span className="min-w-0">
            <span className="block truncate font-medium" data-testid="ctem-signal-sources-path-name">{t_i18n(path.label)}</span>
            {/* Line 2: the description; below 4xl the sources and last signal step in here instead. */}
            {path.description && (
              <span className="hidden truncate content-caption text-default-secondary @4xl:block">{t_i18n(path.description)}</span>
            )}
            <span className="flex min-w-0 items-center gap-3 whitespace-nowrap content-caption text-default-secondary @4xl:hidden">
              <SourceList path={path} connected={connected} compact />
              {path.last_signal_at && <span className="shrink-0">{lastSignal(path)}</span>}
            </span>
          </span>
        </span>
      ),
    },
    {
      key: "sources",
      header: "Sources",
      hideBelow: "4xl",
      hideable: true,
      interactive: true,
      cell: (path) => <SourceList path={path} connected={connected} />,
    },
    {
      key: "status",
      header: "Status",
      width: 168,
      sortValue: (path) => PATH_STATUS_ORDER.indexOf(pathStatus(path)),
      cell: (path) => {
        const status = pathStatus(path);
        return <Chip label={pathStatusLabel(path, t_i18n)} severity={PATH_STATUS_TONE[status]} data-testid="ctem-signal-sources-status" />;
      },
    },
    {
      key: "count_30d",
      header: "Signals (30 days)",
      align: "right",
      width: 136,
      interactive: true,
      hideable: true,
      sortValue: (path) => path.count_30d ?? 0,
      cell: (path) => {
        const byState = (["new", "to_validate", "watchlist", "case_opened"] as const)
          .map((s) => `${t_i18n(SIGNAL_STATE_LABEL[s])} ${exactCount(path.counts?.[s] ?? 0)}`)
          .join(", ");
        const n30 = path.count_30d ?? 0;
        return <CountCell tip={byState} title={exactCount(n30)} label={count(n30)} />;
      },
    },
    {
      key: "last_signal",
      header: "Last signal",
      hideBelow: "4xl",
      hideable: true,
      width: 152,
      sortValue: (path) => path.last_signal_at ?? "",
      cell: (path) => <span className={path.last_signal_at ? undefined : "text-default-secondary"}>{lastSignal(path)}</span>,
    },
    {
      key: "action",
      header: "Actions",
      align: "right",
      width: 160,
      interactive: true,
      cell: (path) => {
        // An enabled path has nothing to switch on; a partly enabled one offers its source that is off.
        const target = pathStatus(path) === "enabled" ? null : connectTarget(path);
        // A profile pass is turned on by an administrator: a reader gets no button for it.
        const actionable = target && (isAdmin || !connectNeedsAdmin(target));
        // A source XTM One provisions, of a connected provider: Set up opens its enable dialog (#2434).
        const setUp = target && isAdmin && isProvisioned(target) && connected.has(target.provider) ? firstOff(target) : null;
        return (
          <span className="inline-flex items-center justify-end gap-1">
            {setUp && (
              <Button variant="secondary" size="sm" onClick={() => setEnabling(setUp)} data-testid="ctem-signal-sources-set-up">
                {t_i18n("Set up")}
              </Button>
            )}
            {actionable && !setUp && !(isProvisioned(target) && connected.has(target.provider)) && (
              <Button variant="secondary" size="sm" onClick={() => navigate(connectHref(target))} data-testid="ctem-signal-sources-connect">
                {target.off ? t_i18n("Turn on") : t_i18n("Connect")}
              </Button>
            )}
            {!target && (path.total ?? 0) > 0 && (
              // A path row opens that path's signals (davidloiret review on #1194): its count is the path's total.
              <Button
                variant="ghost"
                size="sm"
                onClick={() => navigate(signalsHref({ path: path.id }))}
                title={exactCount(path.total)}
                data-testid="ctem-signal-sources-path-open"
              >
                {t_i18n("Open signals")} <span className="tabular-nums">{count(path.total)}</span>
              </Button>
            )}
          </span>
        );
      },
    },
  ];

  return (
    <div data-testid="ctem-signal-sources">
      <DataTable
        rows={paths}
        columns={columns}
        rowKey={(path) => path.id}
        rowLines={2}
        layout="fixed"
        // Twelve paths: one page at the default size, the pager kept like every records table.
        defaultPageSize={25}
        searchFields={(path) => [
          t_i18n(path.label),
          path.description ? t_i18n(path.description) : null,
          ...(path.sources ?? []).map((s) => CTEM_PRODUCT_INFO[s.provider]?.name ?? INTEGRATION_PROVIDER_NAMES[s.provider] ?? s.provider),
        ]}
        filters={[
          {
            key: "status",
            label: "Status",
            values: (path) => [pathStatus(path)],
            options: PATH_STATUS_ORDER.map((s) => ({ value: s, label: t_i18n(PATH_STATUS_LABEL[s]) })),
            // Ordinal scale: connected first, agents-only last.
            ordered: true,
          },
          {
            key: "lens",
            label: "Lens",
            values: (path) => [path.default_lens],
            options: LENSES.map((lens) => ({ value: lens, label: t_i18n(LENS_HEADING[lens]) })),
            // Ordinal scale: the loop's lenses, inside, outside, world.
            ordered: true,
          },
        ]}
        storageKey="ctem-signal-sources"
        noResultsLabel={t_i18n("No trigger path matches these filters")}
        groupBy={{
          value: (path) => path.default_lens,
          rank: (lens) => {
            const at = (LENSES as readonly string[]).indexOf(lens);
            return at < 0 ? LENSES.length : at;
          },
          label: (lens) => (
            <span className="flex min-w-0 items-center gap-2" data-testid={`ctem-signal-sources-lens-${lens}`}>
              <Icon name={(LENS_ICON[lens as keyof typeof LENS_ICON] ?? "activity") as IconName} size={16} className="shrink-0 text-default-secondary" aria-hidden />
              <span className="shrink-0">{t_i18n(LENS_HEADING[lens as keyof typeof LENS_HEADING] ?? lens)}</span>
              {lens in LENS_HELP && (
                <span className="truncate font-normal text-default-secondary">{t_i18n(LENS_HELP[lens as keyof typeof LENS_HELP])}</span>
              )}
            </span>
          ),
        }}
        rowDataAttributes={(path) => ({ "data-path": path.id, "data-status": pathStatus(path) })}
        // A row opens the path's sources, managed in place (#1445).
        onRowClick={(path) => setOpenPath(path.id)}
        isLoading={isLoading}
        isError={isError}
        onRetry={() => void refetch()}
        emptyLabel={t_i18n("No trigger path is known yet.")}
        rowTestId="ctem-signal-sources-path"
      />
      <SignalPathSourcesSheet path={opened} paths={paths} connected={connected} onClose={() => setOpenPath(null)} />
      <PlatformSourceEnableDialog
        source={enabling}
        label={
          enabling
            ? `${CTEM_PRODUCT_INFO[enabling.provider]?.name ?? INTEGRATION_PROVIDER_NAMES[enabling.provider] ?? enabling.provider} ${t_i18n(SOURCE_KIND_LABEL[enabling.kind])}`
            : ""
        }
        onClose={() => setEnabling(null)}
      />
    </div>
  );
}
