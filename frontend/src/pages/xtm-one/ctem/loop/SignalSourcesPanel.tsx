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
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import type { Integration } from "@/types";
import { useAuth } from "@/hooks/useAuth";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { formatShortDateTimeIntl } from "@/lib/relative-time";
import { CTEM_PRODUCT_INFO, INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { SIGNAL_STATE_LABEL } from "./DecisionReason";
import { PlatformSourceEnableDialog } from "./PlatformSourceControls";
import { quietReasonText } from "./quiet-reason";
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
  type SourceState,
  sourceDisplayState,
  sourceIntegrationNames,
  sourceKindText,
  sourceShortName,
  silentReasonText,
  sourceStateIcon,
  SOURCE_STATE_LEGEND,
} from "./signal-sources";

const LENSES = ["inside", "outside", "world"] as const;

/** The lens headings and what each sees. @i18n-keys */
const LENS_HEADING = { inside: "Inside", outside: "Outside", world: "World" } as const;
/** @i18n-keys */
const LENS_HELP = {
  inside: "What your own tools see in your environment",
  outside: "What an attacker sees of you from the internet",
  world: "What happens in the threat landscape that may concern you",
} as const;

/**
 * The empty state of a path with no source on (#2506), on line 2 of the first cell of a folded row: the sentence
 * alone, the row itself opens the path's sources.
 */
function NoSourceOn({ className }: { className?: string }) {
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
    </span>
  );
}

/**
 * The look of a source chip by its state (#2736, Sam 28 Nov 2026): on reads a success outline with its check, error an
 * error outline, not connected a muted dashed outline, every other state (off, not set up, not used here, not
 * configured) a neutral outline. The state word travels in the tooltip and the accessible name; the icon carries it too.
 */
const CHIP_TONE: Record<SourceState, string> = {
  bound: "border-feedback-success-primary/60",
  error: "border-feedback-error-primary/60",
  not_connected: "border-dashed border-elevation-subtle-soft text-default-secondary",
  off: "border-elevation-subtle-soft text-default-secondary",
  excluded: "border-elevation-subtle-soft text-default-secondary",
  not_set_up: "border-elevation-subtle-soft text-default-secondary",
  not_configured: "border-elevation-subtle-soft text-default-secondary",
};

/**
 * One source of a path as a compact chip (#2736): its state icon, provider logo and short name ("GitHub Dependabot",
 * "OpenCTI PIR playbook"); its kind, event, state and integrations in the tooltip. Every source of the path is drawn,
 * a provider with no integration here included, so one look says what is on, off and not connected.
 */
function SourceChip({
  source,
  pathId,
  connected,
  platform,
  integrations,
}: {
  source: CtemSignalPathSource;
  pathId: string;
  connected: ReadonlySet<string>;
  platform: readonly CtemPlatformSource[];
  integrations: readonly Integration[];
}) {
  const { t_i18n } = useFormatter();
  // The row opens the path's sources (#1445): it is the one tab stop, the tooltip triggers add none.
  const tabIndex = useTriggerTabIndex();
  const state = sourceDisplayState(source, connected, platform);
  const name = sourceShortName(source, pathId, providerName, t_i18n);
  const icon = sourceStateIcon(state);
  const event = source.event ? t_i18n(source.event) : null;
  const via = sourceIntegrationNames(source, platform, integrations);
  const tip = [
    `${name} (${sourceKindText(t_i18n, source)}): ${t_i18n(icon.label)}`,
    event,
    via ? `${t_i18n("Integration")}: ${via}` : null,
  ]
    .filter(Boolean)
    .join(". ");
  return (
    <Tip content={tip}>
      <span
        tabIndex={tabIndex}
        aria-label={`${name}: ${t_i18n(icon.label)}`}
        className={cn("inline-flex h-5 shrink-0 items-center gap-1 whitespace-nowrap rounded-sm border px-1.5 content-caption", CHIP_TONE[state])}
        data-testid="ctem-signal-source"
        data-provider={source.provider}
        data-kind={source.kind}
        data-bound={source.bound}
        data-off={source.off ? true : undefined}
        data-state={state}
      >
        <Icon name={icon.icon} size={16} className={cn("shrink-0", icon.className)} aria-hidden data-testid="ctem-signal-source-status-icon" />
        <ProviderLogo provider={source.provider} className={cn("size-3.5 shrink-0", state === "not_connected" && "opacity-60")} decorative />
        {/* Never cut (UX round 1 on #2741, B2): the short name fits whole, the kind is in the tooltip. */}
        <span data-testid="ctem-signal-source-name">
          {name}
        </span>
      </span>
    </Tip>
  );
}

/**
 * The legend of the source chips, once above the table (#2736): one entry per icon, naming every state a chip can
 * show (UX round 1 on #2741, B3).
 */
function ChipLegend() {
  const { t_i18n } = useFormatter();
  return (
    <ul className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 content-caption text-default-secondary" aria-label={t_i18n("Source states")} data-testid="ctem-signal-sources-legend">
      {SOURCE_STATE_LEGEND.map(({ states, label }) => {
        const state: SourceState = states[0];
        const icon = sourceStateIcon(state);
        return (
          <li key={state} className="inline-flex items-center gap-1.5" data-testid="ctem-signal-sources-legend-item" data-icon={icon.icon}>
            <span className={cn("inline-flex size-4 items-center justify-center rounded-sm border", CHIP_TONE[state])} aria-hidden>
              <Icon name={icon.icon} size={16} className={icon.className} />
            </span>
            {t_i18n(label)}
          </li>
        );
      })}
    </ul>
  );
}

/** The info mark beside a 0 count: why the path records nothing, in the DS tooltip (#2642). */
function SilentReasonMark({ text }: { text: string }) {
  const tabIndex = useTriggerTabIndex();
  return (
    <Tip content={text}>
      <span
        tabIndex={tabIndex}
        role="img"
        aria-label={text}
        className="inline-flex shrink-0 items-center rounded-sm text-default-secondary"
        data-testid="ctem-signal-sources-silent-reason"
      >
        <Icon name="info" size={16} aria-hidden />
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

/** The empty Last signal cell with why its pass raised nothing; a keyboard stop like its sibling cells (UX round 1 on #2654, B3). */
function QuietReasonCell({ reason, label }: { reason: string; label: string }) {
  const tabIndex = useTriggerTabIndex();
  return (
    <Tip content={reason}>
      <span className="rounded-sm text-default-secondary" tabIndex={tabIndex} data-testid="ctem-signal-sources-quiet-reason">
        {label}
      </span>
    </Tip>
  );
}

/** The sources-on count with the sources not connected here named in its tooltip (#2736); no tab stop of its own. */
function OnCountCell({ label, tip }: { label: string; tip: string }) {
  const tabIndex = useTriggerTabIndex();
  return (
    <Tip content={tip}>
      <span className="rounded-sm tabular-nums" tabIndex={tabIndex} data-testid="ctem-signal-sources-on-count" data-missing-tip={tip}>
        {label}
      </span>
    </Tip>
  );
}

function providerName(provider: string): string {
  return CTEM_PRODUCT_INFO[provider]?.name ?? INTEGRATION_PROVIDER_NAMES[provider] ?? provider;
}

/** The order of the chips on a row: what records the path first, then what could, then what is not connected. */
const CHIP_ORDER: Record<SourceState, number> = {
  bound: 0,
  error: 1,
  not_set_up: 2,
  not_configured: 3,
  off: 4,
  excluded: 5,
  not_connected: 6,
};

/**
 * The sources of a path (#2736, Sam 28 Nov 2026): every source as a chip, a provider with no integration here
 * included, never "+N more"; a path has at most five, so they wrap to a second line rather than hide. `compact` (line
 * 2 of a folded row) keeps one line: the short names with their state, cut with an ellipsis, the whole list in the
 * tooltip (UX round 1 on #1356, B2).
 */
function SourceList({
  path,
  connected,
  platform,
  integrations,
  className,
  compact,
}: {
  path: CtemSignalPath;
  connected: ReadonlySet<string>;
  platform: readonly CtemPlatformSource[];
  integrations: readonly Integration[];
  className?: string;
  compact?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const tabIndex = useTriggerTabIndex();
  const sources = path.sources ?? [];
  if (sources.length === 0) {
    return <span className={className ? `${className} text-default-secondary` : "text-default-secondary"}>{t_i18n("None")}</span>;
  }
  const stateOf = (s: CtemSignalPathSource) => sourceDisplayState(s, connected, platform);
  const shown = sources
    .map((s, at) => ({ s, at }))
    .sort((a, b) => CHIP_ORDER[stateOf(a.s)] - CHIP_ORDER[stateOf(b.s)] || a.at - b.at)
    .map(({ s }) => s);
  if (compact) {
    if (!shown.some((s) => stateOf(s) === "bound")) return <NoSourceOn className={className} />;
    const label = (s: CtemSignalPathSource) => {
      const icon = sourceStateIcon(stateOf(s));
      return `${sourceShortName(s, path.id, providerName, t_i18n)} (${t_i18n(icon.label)})`;
    };
    const names = shown.map(label).join(", ");
    return (
      <Tip content={names}>
        <span tabIndex={tabIndex} className="flex min-w-0 flex-1 items-center gap-2 rounded-sm" data-testid="ctem-signal-sources-compact">
          <span className="min-w-0 truncate">{names}</span>
        </span>
      </Tip>
    );
  }
  return (
    <span className={cn("flex min-w-0 flex-wrap items-center gap-1", className)} data-testid="ctem-signal-sources-line">
      {shown.map((s) => (
        <SourceChip key={s.key ?? `${s.provider}:${s.kind}:${s.name ?? ""}`} source={s} pathId={path.id} connected={connected} platform={platform} integrations={integrations} />
      ))}
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
  // The integrations each listener XTM One does not provision runs on, named in its chip's tooltip (#2736).
  // A reader whose integrations read fails or answers no list still sees every chip (#2736).
  const { data: integrationList } = useIntegrations();
  const integrations = Array.isArray(integrationList) ? integrationList : [];
  const sourcesOn = (path: CtemSignalPath) =>
    (path.sources ?? []).filter((s) => sourceDisplayState(s, connected, platformSources) === "bound").length;

  const columns: DataTableColumn<CtemSignalPath>[] = [
    {
      key: "path",
      header: "Signal type",
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
              <SourceList path={path} connected={connected} platform={platformSources} integrations={integrations} compact />
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
      // Not interactive (#2736): a click anywhere on the chips opens the path's sources like the rest of the row.
      cell: (path) => <SourceList path={path} connected={connected} platform={platformSources} integrations={integrations} />,
    },
    {
      // Sources on, of every source of the path (#2736): the chips on the row and this count always agree; the
      // sources whose provider has no integration here are named in its tooltip.
      key: "sources_on",
      header: "Sources on",
      align: "right",
      width: 120,
      hideable: true,
      sortValue: (path) => sourcesOn(path),
      cell: (path) => {
        const label = t_i18n("{enabled} of {available}", { enabled: exactCount(sourcesOn(path)), available: exactCount(path.sources?.length ?? 0) });
        const missing = (path.sources ?? []).filter((s) => sourceDisplayState(s, connected, platformSources) === "not_connected");
        if (missing.length === 0) {
          return (
            <span className="tabular-nums" data-testid="ctem-signal-sources-on-count">
              {label}
            </span>
          );
        }
        const names = [...new Set(missing.map((s) => providerName(s.provider)))].join(", ");
        return (
          <OnCountCell
            label={label}
            tip={t_i18n("{count, plural, other {# not connected: {names}}}", { count: missing.length, names })}
          />
        );
      },
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
      // Wide enough for the header and its sort mark at 1728; the rest goes to the Sources chips (UX round 1 on #2741, B1).
      width: 152,
      // Not interactive (#2736): the count opens the path's sources like the rest of the row; its tooltip stays.
      hideable: true,
      sortValue: (path) => path.count_30d ?? 0,
      cell: (path) => {
        const byState = (["new", "to_validate", "watchlist", "case_opened"] as const)
          .map((s) => `${t_i18n(SIGNAL_STATE_LABEL[s])} ${exactCount(path.counts?.[s] ?? 0)}`)
          .join(", ");
        const n30 = path.count_30d ?? 0;
        const cell = <CountCell tip={byState} title={exactCount(n30)} label={count(n30)} />;
        // A path that is on with 0 signals says why beside its count (#2642).
        if (!path.silent_reason) return cell;
        const why = silentReasonText(path.silent_reason, t_i18n, (iso) => formatShortDateTimeIntl(intl, iso));
        return (
          <span className="inline-flex items-center justify-end gap-1.5">
            <SilentReasonMark text={why} />
            {cell}
          </span>
        );
      },
    },
    {
      key: "last_signal",
      header: "Last signal",
      hideBelow: "4xl",
      hideable: true,
      width: 152,
      sortValue: (path) => path.last_signal_at ?? "",
      cell: (path) => {
        // Why an empty outside path raised nothing (#2634): the reason its pass gives, worded here, on the empty cell.
        const reason = !path.last_signal_at
          ? quietReasonText(path.quiet_reason, t_i18n, (iso) => formatShortDateTimeIntl(intl, iso))
          : undefined;
        return reason ? (
          <QuietReasonCell reason={reason} label={lastSignal(path)} />
        ) : (
          <span className={path.last_signal_at ? undefined : "text-default-secondary"}>{lastSignal(path)}</span>
        );
      },
    },
    {
      key: "action",
      header: "Actions",
      align: "right",
      width: 160,
      // Not interactive (#2736): the empty part of the cell opens the row; each button stops its own click.
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
              <Button variant="secondary" size="sm" onClick={(event) => { event.stopPropagation(); setEnabling(setUp); }} data-testid="ctem-signal-sources-set-up">
                {t_i18n("Set up")}
              </Button>
            )}
            {actionable && !setUp && !(isProvisioned(target) && connected.has(target.provider)) && (
              <Button variant="secondary" size="sm" onClick={(event) => { event.stopPropagation(); navigate(connectHref(target)); }} data-testid="ctem-signal-sources-connect">
                {target.off ? t_i18n("Turn on") : t_i18n("Connect")}
              </Button>
            )}
            {!target && (path.total ?? 0) > 0 && (
              // A path row opens that path's signals (davidloiret review on #1194): its count is the path's total.
              <Button
                variant="ghost"
                size="sm"
                onClick={(event) => {
                  event.stopPropagation();
                  navigate(signalsHref({ path: path.id }));
                }}
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
      <ChipLegend />
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
          ...(path.sources ?? []).map((s) => sourceShortName(s, path.id, providerName, t_i18n)),
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
