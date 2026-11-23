// SPDX-License-Identifier: Apache-2.0

/**
 * The signal sources (#1189, dev-docs/ctem-signals.md section 2bis):
 * every trigger path the loop knows, read from `GET /ctem-signals/paths` and
 * never hard-coded, as a compact table grouped by lens (Sam, 23 Nov 2026, #1344):
 * trigger path (label, description on line 2), sources (logo and name, the kind
 * as secondary text, the event that fires it in a tooltip), status (its own
 * column so every chip aligns: Connected, Off, Not connected, or Agents and API
 * only, a path with no automatic source never reading connected), signals in 30
 * days, last signal, and one action: Connect or Turn on the first unbound
 * source, or Open signals in the Signals tab. Rows of at most two lines; below
 * the 4xl container width the Sources and Last signal columns step into line 2
 * of the first cell.
 *
 * The Sources tab of the Signals page (`/ctem/signals/sources`, #1263), reached
 * from the page's tabs and summary strip.
 */
import { useNavigate } from "react-router-dom";
import { Chip, Icon, type IconName } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { Tip } from "@/components/ui/tooltip";
import { useCtemSignalPaths, type CtemSignalPath, type CtemSignalPathSource } from "@/hooks/api/useCtemSignalsApi";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { formatShortDateTimeIntl } from "@/lib/relative-time";
import { CTEM_PRODUCT_INFO, INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { SIGNAL_STATE_LABEL } from "./DecisionReason";
import { signalsHref } from "./signals-route";
import {
  connectHref,
  connectTarget,
  LENS_ICON,
  PATH_ICON,
  PATH_STATUS_LABEL,
  PATH_STATUS_TONE,
  pathStatus,
  SOURCE_KIND_LABEL,
} from "./signal-sources";

const LENSES = ["inside", "outside", "world"] as const;

/** The status order of the sort and the filter: connected first, agents-only last. */
const STATUS_ORDER = ["connected", "off", "not_connected", "agents_api"] as const;

/** The lens headings and what each sees. @i18n-keys */
const LENS_HEADING = { inside: "Inside", outside: "Outside", world: "World" } as const;
/** @i18n-keys */
const LENS_HELP = {
  inside: "What your own tools see in your environment",
  outside: "What an attacker sees of you from the internet",
  world: "What happens in the threat landscape that may concern you",
} as const;

/** One source: logo and name, the kind as secondary text, the event that fires it in a tooltip (never a third line). */
function SourceItem({ source }: { source: CtemSignalPathSource }) {
  const { t_i18n } = useFormatter();
  // The loop trace's naming (signalProviderName), read here without importing that module: it imports this one.
  const name = CTEM_PRODUCT_INFO[source.provider]?.name ?? INTEGRATION_PROVIDER_NAMES[source.provider] ?? source.provider;
  return (
    <Tip content={source.event ? t_i18n(source.event) : null}>
      <span
        tabIndex={0}
        className="inline-flex min-w-0 items-center gap-1.5 rounded-sm"
        data-testid="ctem-signal-source"
        data-provider={source.provider}
        data-kind={source.kind}
        data-bound={source.bound}
        data-off={source.off ? true : undefined}
      >
        <ProviderLogo provider={source.provider} className="size-4 shrink-0" decorative />
        <span className={source.bound ? "truncate" : "truncate text-default-secondary"}>{name}</span>
        <span className="shrink-0 content-caption text-default-secondary">{t_i18n(SOURCE_KIND_LABEL[source.kind])}</span>
      </span>
    </Tip>
  );
}

function SourceList({ path, className }: { path: CtemSignalPath; className?: string }) {
  const { t_i18n } = useFormatter();
  const sources = path.sources ?? [];
  if (sources.length === 0) {
    return <span className={className ? `${className} text-default-secondary` : "text-default-secondary"}>{t_i18n("None")}</span>;
  }
  return (
    <span className={className ? `${className} flex min-w-0 flex-wrap gap-x-3` : "flex min-w-0 flex-wrap gap-x-3"}>
      {sources.map((s) => (
        <SourceItem key={`${s.provider}:${s.kind}`} source={s} />
      ))}
    </span>
  );
}

/**
 * The Sources tab of the Signals page (#1263): every trigger path by lens,
 * its status, sources, signals in 30 days and Connect. A path's "Open signals"
 * opens the Signals tab filtered on it.
 */
export function SignalSourcesPanel() {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const { count, exactCount } = useNumberFormat();
  const { data, isLoading, isError, refetch } = useCtemSignalPaths();
  const paths = data?.paths ?? [];
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
            <span className="flex min-w-0 items-center gap-3 truncate content-caption text-default-secondary @4xl:hidden">
              <SourceList path={path} />
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
      cell: (path) => <SourceList path={path} />,
    },
    {
      key: "status",
      header: "Status",
      width: 168,
      sortValue: (path) => STATUS_ORDER.indexOf(pathStatus(path)),
      cell: (path) => {
        const status = pathStatus(path);
        return <Chip label={t_i18n(PATH_STATUS_LABEL[status])} severity={PATH_STATUS_TONE[status]} data-testid="ctem-signal-sources-status" />;
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
        return (
          <Tip content={byState}>
            <span className="tabular-nums" tabIndex={0} title={exactCount(n30)} data-testid="ctem-signal-sources-30d">
              {count(n30)}
            </span>
          </Tip>
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
      cell: (path) => <span className={path.last_signal_at ? undefined : "text-default-secondary"}>{lastSignal(path)}</span>,
    },
    {
      key: "action",
      header: "Actions",
      align: "right",
      width: 160,
      interactive: true,
      cell: (path) => {
        const target = pathStatus(path) === "connected" ? null : connectTarget(path);
        return (
          <span className="inline-flex items-center justify-end gap-1">
            {target && (
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
            options: STATUS_ORDER.map((s) => ({ value: s, label: t_i18n(PATH_STATUS_LABEL[s]) })),
            // An ordinal scale: connected first, agents-only last.
            ordered: true,
          },
          {
            key: "lens",
            label: "Lens",
            values: (path) => [path.default_lens],
            options: LENSES.map((lens) => ({ value: lens, label: t_i18n(LENS_HEADING[lens]) })),
            // The loop's order: inside, outside, world.
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
        isLoading={isLoading}
        isError={isError}
        onRetry={() => void refetch()}
        emptyLabel={t_i18n("No trigger path is known yet.")}
        rowTestId="ctem-signal-sources-path"
      />
    </div>
  );
}
