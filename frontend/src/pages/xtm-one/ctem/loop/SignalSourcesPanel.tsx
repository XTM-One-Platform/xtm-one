// SPDX-License-Identifier: Apache-2.0

/**
 * The signal sources (#1189, dev-docs/ctem-signals.md section 2bis):
 * every trigger path the loop knows, read from `GET /ctem-signals/paths` and
 * never hard-coded, as a compact table grouped by lens (Sam, 23 Nov 2026, #1344):
 * trigger path (label, description on line 2), sources (logo and name, the kind
 * as secondary text, the event that fires it in a tooltip; only what feeds the
 * path on this platform, a provider with no integration here never listed: the
 * sources count and the path's sources panel carry it, #1401, #2506), sources on
 * of available (#2506), status (its own
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
  sourceDisplayName,
  sourceDisplayState,
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
 * The empty state of a path with no source on (#2506): what it means, and the way to its sources. On line 2 of
 * the first cell (`compact`) it is the sentence alone: the row itself opens the path's sources.
 */
function NoSourceOn({ onOpen, className, compact }: { onOpen: () => void; className?: string; compact?: boolean }) {
  const { t_i18n } = useFormatter();
  const tabIndex = useTriggerTabIndex();
  return (
    <span className={cn("flex min-w-0 items-center gap-2", className)} data-testid="ctem-signal-sources-none-on">
      {/* Cut at narrow widths: the whole sentence in the DS tooltip (UX round 1 on #2509, B3). */}
      <Tip content={t_i18n("No source on: nothing records this path")}>
        <span tabIndex={tabIndex} className="min-w-0 truncate rounded-sm text-default-secondary" data-testid="ctem-signal-sources-none-on-text">
          {t_i18n("No source on: nothing records this path")}
        </span>
      </Tip>
      {!compact && (
      <Button
        variant="ghost"
        size="sm"
        tabIndex={tabIndex}
        className="shrink-0"
        onClick={(event) => {
          event.stopPropagation();
          onOpen();
        }}
        data-testid="ctem-signal-sources-open-sources"
      >
        {t_i18n("Open its sources")}
      </Button>
      )}
    </span>
  );
}

/**
 * One source: logo and name, the kind as secondary text, the event that fires it in a tooltip (never a third line).
 * A source turned off says Off after its kind; a source of a connected provider that is not set up says Not set up
 * (UX round 1 on #1405, B1). Only the sources of a connected provider are listed (#2506).
 */
function SourceItem({
  source,
  connected,
  platform,
}: {
  source: CtemSignalPathSource;
  connected: ReadonlySet<string>;
  platform: readonly CtemPlatformSource[];
}) {
  const { t_i18n } = useFormatter();
  // The loop trace's naming (signalProviderName), read here without importing that module: it imports this one.
  const name = sourceName(source);
  // The row opens the path's sources (#1445): it is the one tab stop, the tooltip triggers add none.
  const tabIndex = useTriggerTabIndex();
  // One word per source on the row and in the path's sources panel (UX advisory A4 of #2436).
  const state = sourceDisplayState(source, connected, platform);
  const event = source.event ? t_i18n(source.event) : null;
  const icon = sourceStateIcon(state);
  const word = state in COMPACT_STATE_WORD ? COMPACT_STATE_WORD[state as keyof typeof COMPACT_STATE_WORD] : null;
  // The tooltip names the source first, as MoreSources and the folded variant do (UX round 2 on #2509, B1): the name
  // can be cut on a narrow cell, so it is always readable here, then the event that fires it.
  const heading = `${name} (${sourceKindText(t_i18n, source)}${word ? `, ${t_i18n(word)}` : ""})`;
  const stateLine = icon && !word ? t_i18n(icon.label) : null;
  const tip = [heading, event, stateLine].filter(Boolean).join(". ");
  return (
    <Tip content={tip}>
      <span
        tabIndex={tabIndex}
        className="inline-flex h-5 min-w-0 flex-wrap content-start items-center gap-x-1.5 gap-y-2 overflow-hidden rounded-sm"
        data-testid="ctem-signal-source"
        data-provider={source.provider}
        data-kind={source.kind}
        data-bound={source.bound}
        data-off={source.off ? true : undefined}
        data-state={state}
      >
        {/* Icons and name stay together on line 1 whatever the name's length: the group is the first item of the line, so
            it never wraps, and the name shrinks with an ellipsis inside it (up to 12rem); only a caption that does not fit
            whole wraps onto the hidden second line, the tooltip carrying it (UX rounds 2 and 3 on #2509, B1). */}
        <span className="inline-flex min-w-0 max-w-full flex-nowrap items-center gap-x-1.5" data-testid="ctem-signal-source-head">
          {icon && <Icon name={icon.icon} size={16} className={cn("shrink-0", icon.className)} aria-hidden data-testid="ctem-signal-source-status-icon" />}
          <ProviderLogo provider={source.provider} className="size-4 shrink-0" decorative />
          <span className={cn("min-w-0 max-w-48 truncate", !source.bound && "text-default-secondary")} data-testid="ctem-signal-source-name">
            {name}
          </span>
        </span>
        <span className="shrink-0 whitespace-nowrap content-caption text-default-secondary">{sourceKindText(t_i18n, source)}</span>
        {state === "off" && <span className="shrink-0 whitespace-nowrap content-caption text-default-secondary">{t_i18n("Off")}</span>}
        {state === "excluded" && <span className="shrink-0 whitespace-nowrap content-caption text-default-secondary">{t_i18n("Not used here")}</span>}
        {state === "not_set_up" && <span className="shrink-0 whitespace-nowrap content-caption text-default-secondary">{t_i18n("Not set up")}</span>}
        {state === "error" && <span className="shrink-0 whitespace-nowrap content-caption text-feedback-error-primary">{t_i18n("Error")}</span>}
      </span>
    </Tip>
  );
}

/** The sources not used on this path: one muted item, the list in its tooltip (UX round 1 on #2454, B1). */
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

/** How many sources a wide row lists before "+N more" (UX round 1 on #2509, B1). */
const INLINE_SOURCES = 1;

/** The sources past the first ones of a wide row: one muted item, the list with each state in its tooltip. */
function MoreSources({
  sources,
  connected,
  platform,
}: {
  sources: CtemSignalPathSource[];
  connected: ReadonlySet<string>;
  platform: readonly CtemPlatformSource[];
}) {
  const { t_i18n } = useFormatter();
  const tabIndex = useTriggerTabIndex();
  const tip = (
    <span className="flex flex-col gap-0.5">
      {sources.map((s) => {
        const state = sourceDisplayState(s, connected, platform);
        const word = state in COMPACT_STATE_WORD ? COMPACT_STATE_WORD[state as keyof typeof COMPACT_STATE_WORD] : null;
        return (
          <span key={s.key ?? `${s.provider}:${s.kind}`}>
            {`${sourceName(s)} (${sourceKindText(t_i18n, s)}${word ? `, ${t_i18n(word)}` : ""})`}
          </span>
        );
      })}
    </span>
  );
  return (
    <Tip content={tip}>
      <span
        tabIndex={tabIndex}
        className="inline-flex shrink-0 items-center rounded-sm content-caption text-default-secondary"
        data-testid="ctem-signal-sources-more"
      >
        {t_i18n("{count, plural, other {+# more}}", { count: sources.length })}
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

function providerName(provider: string): string {
  return CTEM_PRODUCT_INFO[provider]?.name ?? INTEGRATION_PROVIDER_NAMES[provider] ?? provider;
}

function sourceName(source: CtemSignalPathSource): string {
  return sourceDisplayName(source, providerName);
}

/**
 * The sources of a path. `compact` (line 2 of a folded row) keeps one line: the names
 * cut with an ellipsis, the whole list in the tooltip, never a wrap to a third line
 * (UX round 1 on #1356, B2).
 */
function SourceList({
  path,
  connected,
  platform,
  onOpen,
  className,
  compact,
}: {
  path: CtemSignalPath;
  connected: ReadonlySet<string>;
  platform: readonly CtemPlatformSource[];
  onOpen: () => void;
  className?: string;
  compact?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const tabIndex = useTriggerTabIndex();
  const sources = path.sources ?? [];
  if (sources.length === 0) {
    return <span className={className ? `${className} text-default-secondary` : "text-default-secondary"}>{t_i18n("None")}</span>;
  }
  // Only the sources of a connected provider are listed, each saying its own state (UX round 1 on #1405, B1); a
  // provider with no integration here is never listed nor counted as "possible" (#2506): the sources count and the
  // path's sources panel carry it. A path with no source on reads so and opens its sources (#2506).
  const { live } = splitSources(path, connected);
  const anyOn = live.some((s) => sourceDisplayState(s, connected, platform) === "bound");
  if (!anyOn) return <NoSourceOn onOpen={onOpen} className={className} compact={compact} />;
  // The sources that are on come first, so the row says what records the path, folded or wide (UX round 2 advisory
  // A2 on #2509); the order is otherwise kept.
  const isOn = (s: CtemSignalPathSource) => sourceDisplayState(s, connected, platform) === "bound";
  const shown = [...live.filter(isOn), ...live.filter((s) => !isOn(s))];
  if (compact) {
    const stateWord = (s: CtemSignalPathSource) => {
      const state = sourceDisplayState(s, connected, platform);
      const word = state in COMPACT_STATE_WORD ? COMPACT_STATE_WORD[state as keyof typeof COMPACT_STATE_WORD] : null;
      return word ? `, ${t_i18n(word)}` : "";
    };
    const label = (s: CtemSignalPathSource) => `${sourceName(s)} (${sourceKindText(t_i18n, s)}${stateWord(s)})`;
    const names = shown.map(label).join(", ");
    return (
      <Tip content={names}>
        <span tabIndex={tabIndex} className="flex min-w-0 flex-1 items-center gap-2 rounded-sm" data-testid="ctem-signal-sources-compact">
          <span className="min-w-0 truncate">{names}</span>
        </span>
      </Tip>
    );
  }
  // A source not used on this path folds into one muted "+N not used here" while something else is listed, so the
  // cell keeps to two lines (UX round 1 on #2454, B1: the default-off named sources wrapped a row to a third line).
  const notUsed = (s: CtemSignalPathSource) => sourceState(s, connected) === "excluded";
  const unused = shown.some((s) => !notUsed(s)) ? shown.filter(notUsed) : [];
  const listed = unused.length > 0 ? shown.filter((s) => !notUsed(s)) : shown;
  // One line, never a third row height (UX round 1 on #2509, B1): the first source, then "+N more" with the
  // rest in the DS tooltip, as the folded variant does. Two sources with their kind and state never fit the cell.
  const inline = listed.slice(0, INLINE_SOURCES);
  const more = listed.slice(INLINE_SOURCES);
  return (
    <span className={cn("flex min-w-0 flex-nowrap items-center gap-x-3 overflow-hidden", className)} data-testid="ctem-signal-sources-line">
      {inline.map((s) => (
        <SourceItem key={s.key ?? `${s.provider}:${s.kind}`} source={s} connected={connected} platform={platform} />
      ))}
      {more.length > 0 && <MoreSources sources={more} connected={connected} platform={platform} />}
      {unused.length > 0 && <UnusedSources sources={unused} />}
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
              <SourceList path={path} connected={connected} platform={platformSources} onOpen={() => setOpenPath(path.id)} compact />
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
      cell: (path) => <SourceList path={path} connected={connected} platform={platformSources} onOpen={() => setOpenPath(path.id)} />,
    },
    {
      // Sources on, of the sources that are or could be on here (#2506): a provider with no integration counts in neither.
      key: "sources_on",
      header: "Sources on",
      align: "right",
      width: 120,
      hideable: true,
      sortValue: (path) => path.sources_enabled ?? 0,
      cell: (path) => (
        <span className="tabular-nums" data-testid="ctem-signal-sources-on-count">
          {t_i18n("{enabled} of {available}", { enabled: exactCount(path.sources_enabled ?? 0), available: exactCount(path.sources_available ?? 0) })}
        </span>
      ),
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
      // Wide enough for the header and its sort mark at 1728 in every language (UX advisory A3 of #2436).
      width: 176,
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
          ...(path.sources ?? []).map((s) => sourceName(s)),
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
