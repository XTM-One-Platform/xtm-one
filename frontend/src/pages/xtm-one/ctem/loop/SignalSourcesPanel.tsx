// SPDX-License-Identifier: Apache-2.0

/**
 * The signal sources (#1189, dev-docs/ctem-signals.md section 2bis):
 * every trigger path the loop knows, read from `GET /ctem-signals/paths` and
 * never hard-coded, grouped by lens. Each path says what fires it, whether a
 * source is connected (Connected, Not connected, or Agents and API only: a path
 * with no automatic source never reads connected), the sources with their logos
 * and the event that fires each, its signals of the last 30 days, and one
 * action: Connect the first unbound source, or Open signals in the loop trace.
 *
 * The Sources tab of the Signals page (`/ctem/signals/sources`, #1263), reached
 * from the icon button next to the signals counter, an empty signal list and
 * the trigger paths card.
 */
import { Link as RouterLink, useNavigate } from "react-router-dom";
import { Chip, Icon, type IconName } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { Tip } from "@/components/ui/tooltip";
import { useCtemSignalPaths, type CtemSignalPath, type CtemSignalPathSource } from "@/hooks/api/useCtemSignalsApi";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { formatShortDateTimeIntl } from "@/lib/relative-time";
import { CTEM_PRODUCT_INFO, INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { cn } from "@/lib/utils";
import { SIGNAL_STATE_LABEL } from "./DecisionReason";
import { SIGNAL_SOURCES_PATH, signalsHref } from "./signals-route";
import {
  connectHref,
  connectTarget,
  connectedCount,
  LENS_ICON,
  PATH_ICON,
  PATH_STATUS_LABEL,
  PATH_STATUS_TONE,
  pathStatus,
  SOURCE_KIND_LABEL,
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
 * What the signal sources button's tooltip says (#1263): the paths connected
 * of the catalogue, then connected of total per lens, then the hint.
 */
export function SignalSourcesTip({ paths }: { paths: readonly CtemSignalPath[] }) {
  const { t_i18n } = useFormatter();
  const { count } = useNumberFormat();
  return (
    <div className="flex min-w-48 flex-col gap-1.5" data-testid="ctem-signal-sources-tip">
      <span className="font-medium">{t_i18n("Signal sources")}</span>
      {paths.length > 0 && (
        <>
          <span>
            {t_i18n("{connected} of {total} trigger paths connected", {
              connected: count(connectedCount(paths)),
              total: count(paths.length),
            })}
          </span>
          <ul className="flex flex-col gap-0.5">
            {LENSES.map((lens) => {
              const inLens = paths.filter((p) => p.default_lens === lens);
              if (inLens.length === 0) return null;
              return (
                <li key={lens} className="flex items-center gap-2 tabular-nums" data-testid={`ctem-signal-sources-tip-${lens}`}>
                  <Icon name={LENS_ICON[lens] as IconName} size={16} aria-hidden />
                  <span className="flex-1">{t_i18n(LENS_HEADING[lens])}</span>
                  <span>{t_i18n("{connected} of {total}", { connected: count(connectedCount(inLens)), total: count(inLens.length) })}</span>
                </li>
              );
            })}
          </ul>
        </>
      )}
      <span className="opacity-80">{t_i18n("Open signal sources")}</span>
    </div>
  );
}

/**
 * The icon button that opens the Sources tab of the Signals page (#1263), next
 * to the signals counter and in card headers. Primary, so it reads as the way
 * in to the sources; its tooltip shows on hover and keyboard focus.
 */
export function SignalSourcesButton({ className }: { className?: string }) {
  const { t_i18n } = useFormatter();
  const { data } = useCtemSignalPaths();
  return (
    <Tip content={<SignalSourcesTip paths={data?.paths ?? []} />}>
      <RouterLink
        to={SIGNAL_SOURCES_PATH}
        aria-label={t_i18n("Signal sources")}
        data-testid="ctem-signal-sources-open"
        className={cn("inline-flex size-6 shrink-0 items-center justify-center rounded-sm text-accent hover:bg-elevation-hover", BUTTON_ROLE_FOCUS_RING, className)}
      >
        <Icon name="satellite-dish" size={16} aria-hidden />
      </RouterLink>
    </Tip>
  );
}

function SourceLine({ source }: { source: CtemSignalPathSource }) {
  const { t_i18n } = useFormatter();
  // The loop trace's naming (signalProviderName), read here without importing that module: it imports this one.
  const name = CTEM_PRODUCT_INFO[source.provider]?.name ?? INTEGRATION_PROVIDER_NAMES[source.provider] ?? source.provider;
  return (
    <li className="flex min-w-0 items-start gap-2 content-caption" data-testid="ctem-signal-source" data-provider={source.provider} data-kind={source.kind} data-bound={source.bound}>
      <ProviderLogo provider={source.provider} className="mt-0.5 size-4 shrink-0" decorative />
      <span className="min-w-0">
        <span className={cn("font-medium", !source.bound && "text-default-secondary")}>{name}</span>
        <span className="text-default-secondary"> · {t_i18n(SOURCE_KIND_LABEL[source.kind])}</span>
        <span className="block text-default-secondary">{t_i18n(source.event)}</span>
      </span>
    </li>
  );
}

function PathCard({ path, onOpenSignals }: { path: CtemSignalPath; onOpenSignals: (pathId: string) => void }) {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const { count, exactCount } = useNumberFormat();
  const status = pathStatus(path);
  const target = status === "connected" ? null : connectTarget(path);
  const sources = path.sources ?? [];
  const n30 = path.count_30d ?? 0;
  const byState = (["new", "to_validate", "watchlist", "case_opened"] as const)
    .map((s) => `${t_i18n(SIGNAL_STATE_LABEL[s])} ${exactCount(path.counts?.[s] ?? 0)}`)
    .join(", ");
  return (
    <li
      className="flex min-w-0 flex-col gap-2 py-3"
      data-testid="ctem-signal-sources-path"
      data-path={path.id}
      data-status={status}
    >
      <div className="flex min-w-0 items-start gap-3">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-sm bg-elevation-hover text-accent" aria-hidden>
          <Icon name={(PATH_ICON[path.id] ?? "activity") as IconName} size={16} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <h4 className="min-w-0 content-compact font-medium">{t_i18n(path.label)}</h4>
            <Chip label={t_i18n(PATH_STATUS_LABEL[status])} severity={PATH_STATUS_TONE[status]} className="shrink-0" data-testid="ctem-signal-sources-status" />
          </div>
          {path.description && <p className="content-caption text-default-secondary">{t_i18n(path.description)}</p>}
        </div>
      </div>
      {sources.length > 0 ? (
        <ul className="flex flex-col gap-1.5 pl-11">
          {sources.map((s) => (
            <SourceLine key={`${s.provider}:${s.kind}`} source={s} />
          ))}
        </ul>
      ) : (
        <p className="pl-11 content-caption text-default-secondary">
          {t_i18n("No automatic source yet: agents and the API can record it.")}
        </p>
      )}
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 pl-11 content-caption text-default-secondary">
        <Tip content={byState}>
          <span className="tabular-nums" tabIndex={0} data-testid="ctem-signal-sources-30d">
            {t_i18n("{count, plural, one {# signal} other {# signals}} in 30 days", { count: n30 })}
          </span>
        </Tip>
        {path.last_signal_at && (
          <span>{t_i18n("Last signal {date}", { date: formatShortDateTimeIntl(intl, path.last_signal_at) })}</span>
        )}
        <span className="ml-auto flex shrink-0 items-center gap-1">
          {target && (
            <Button variant="secondary" size="sm" onClick={() => navigate(connectHref(target))} data-testid="ctem-signal-sources-connect">
              {t_i18n("Connect")}
            </Button>
          )}
          {(path.total ?? 0) > 0 && (
            <Button variant="ghost" size="sm" onClick={() => onOpenSignals(path.id)} title={exactCount(path.total)} data-testid="ctem-signal-sources-path-open">
              {t_i18n("Open signals")} <span className="tabular-nums">{count(path.total)}</span>
            </Button>
          )}
        </span>
      </div>
    </li>
  );
}

/**
 * The Sources tab of the Signals page (#1263): every trigger path by lens,
 * its status, sources, signals in 30 days and Connect. A path's "Open signals"
 * opens the Signals tab filtered on it.
 */
export function SignalSourcesPanel() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { data, isLoading, isError } = useCtemSignalPaths();
  const paths = data?.paths ?? [];
  // A path card opens that path's signals (davidloiret review on #1194): its count is the path's total.
  const openSignals = (pathId: string) => navigate(signalsHref({ path: pathId }));
  if (isLoading) return <p className="content-compact text-default-secondary">{t_i18n("Loading...")}</p>;
  if (isError) return <p className="content-compact text-default-secondary">{t_i18n("The signal sources could not be read. Try again in a moment.")}</p>;
  return (
    <div className="flex flex-col gap-5" data-testid="ctem-signal-sources">
      {LENSES.map((lens) => {
        const inLens = paths.filter((p) => p.default_lens === lens);
        if (inLens.length === 0) return null;
        return (
          <section key={lens} className="flex min-w-0 flex-col gap-2" data-testid={`ctem-signal-sources-lens-${lens}`}>
            <div className="flex items-center gap-2">
              <Icon name={LENS_ICON[lens] as IconName} size={16} className="text-default-secondary" aria-hidden />
              <h2 className="content-compact font-medium">{t_i18n(LENS_HEADING[lens])}</h2>
              <span className="content-caption text-default-secondary">{t_i18n(LENS_HELP[lens])}</span>
            </div>
            <ul className="divide-y divide-[var(--border-elevation-subtle)]">
              {inLens.map((p) => (
                <PathCard key={p.id} path={p} onOpenSignals={openSignals} />
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

/** The link an empty signal list shows: the Sources tab. */
export function SignalSourcesLink() {
  const { t_i18n } = useFormatter();
  return (
    <RouterLink
      to={SIGNAL_SOURCES_PATH}
      data-testid="ctem-signal-sources-link"
      className={cn("inline-flex items-center gap-1.5 rounded-sm content-compact text-accent hover:underline", BUTTON_ROLE_FOCUS_RING)}
    >
      <Icon name="satellite-dish" size={16} aria-hidden />
      {t_i18n("See which signal sources are connected")}
    </RouterLink>
  );
}
