// SPDX-License-Identifier: Apache-2.0

/**
 * The sources of one trigger path, managed in place (#1445, dev-docs/ctem-signals.md section 2bis).
 *
 * Opened from a row of the signal sources page, as a wide panel holding one paginated table (#2506): every
 * source of the path, connected or not, with its kind, integration, run-as, status (the path list's word, A4 of
 * #2436), 30-day signals and last signal, filtered by provider and status. A row opens its configuration, the
 * control that exists for it today:
 * - every connected source: "Use on this path", the per path exclusion of the organization profile
 *   (`signal_sources.excluded`); taking it off this path never turns it off for its other paths;
 * - a scheduled pass of the profile (attack surface, evidence health): its organization-wide switch
 *   (the same `enabled` its Settings card writes), the integrations it runs on, the paths it also
 *   feeds and, for the attack surface pass, a pointer when no owned domain or range is set;
 * - a live listener that needs nothing provisioned: its state only (it runs while its integration is
 *   connected);
 * - a source XTM One provisions in its platform (the OpenAEV and OpenCTI live listeners, the PIR
 *   playbook, #2434): per integration its switch, run-as, status and reason; "Set up" and the switch
 *   open its enable dialog, where the run-as is picked (`PlatformSourceControls`);
 * - a provider with no integration here: Connect, the integration creation flow;
 * - a generic signed webhook source (#2506): its switch on the row, and its configure / rotate / test dialog
 *   (`WebhookSourceDialog`); "Add a signed webhook" creates one, off.
 *
 * Every write is an organization profile save, so administrators only: a reader sees the same states
 * without controls.
 */
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { Chip, Icon, type IconName } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { SlideOver } from "@/components/ui/slide-over";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import {
  type CtemPlatformSource,
  type CtemSignalPath,
  type CtemSignalPathSource,
  type CtemWebhookSource,
  useCtemPlatformSources,
  useCtemWebhookSources,
  useUpdateCtemWebhookSource,
} from "@/hooks/api/useCtemSignalsApi";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { type OrganizationProfile, useOrganizationContext, usePatchOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import { useAuth } from "@/hooks/useAuth";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { CTEM_PRODUCT_INFO, INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { formatShortDateTimeIntl } from "@/lib/relative-time";
import {
  DEFAULT_ATTACK_SURFACE,
  DEFAULT_EVIDENCE_HEALTH,
  type EvidenceHealthPass,
  evidenceHealthRuns,
  evidenceHealthSavedIntegration,
  passIntegrationIds,
} from "@/lib/organization-profile";
import { cn } from "@/lib/utils";
import { PlatformSourceControls, PlatformSourceEnableDialog, RunAsName } from "./PlatformSourceControls";
import { isProvisioned, platformSourceFixRoute, sourceErrorLines, sourceReasonLine } from "./platform-sources";
import { PirSelection } from "./PirSelection";
import { WebhookSourceDialog } from "./WebhookSourceDialog";
import {
  PATH_ICON,
  PATH_STATUS_TONE,
  SOURCE_KIND_LABEL,
  sourceKindText,
  canExclude,
  connectHref,
  connectNeedsAdmin,
  pathStatus,
  pathStatusLabel,
  profilePassField,
  type SourceState,
  sourceShortName,
  sourceIntegrationList,
  sourceDisplayState,
  sourceIntegrationNames,
  sourceKey,
  sourceState,
  sourceStateIcon,
  withExclusion,
} from "./signal-sources";

function providerName(provider: string): string {
  return CTEM_PRODUCT_INFO[provider]?.name ?? INTEGRATION_PROVIDER_NAMES[provider] ?? provider;
}

/** The configuration of one source, drawn in its opened row of the path table. */
function SourceConfig({
  path,
  source,
  connected,
  labels,
  profile,
  isAdmin,
  saving,
  onSave,
  platformSources,
  onEnable,
}: {
  path: CtemSignalPath;
  source: CtemSignalPathSource;
  connected: ReadonlySet<string>;
  labels: Record<string, string>;
  profile: OrganizationProfile | undefined;
  isAdmin: boolean;
  saving: boolean;
  onSave: (next: Partial<OrganizationProfile>, done: string) => void;
  platformSources: readonly CtemPlatformSource[];
  onEnable: (source: CtemPlatformSource) => void;
}) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { data: integrations = [] } = useIntegrations();
  const state = sourceState(source, connected);
  const passField = profilePassField(source);
  const key = sourceKey(source);
  const shared = (source.also_feeds ?? []).map((id) => labels[id] ?? id);
  const provisioned = isProvisioned(source);
  // "Set up" opens the enable dialog of the first integration of the provider still off (#2434).
  const firstOff = platformSources.find((s) => s.provider === source.provider && s.kind === source.kind && !s.enabled && s.integration_connected);

  const attackSurface = { ...DEFAULT_ATTACK_SURFACE, ...(profile?.attack_surface ?? {}) };
  const evidenceHealth: EvidenceHealthPass = { ...DEFAULT_EVIDENCE_HEALTH, ...((profile?.evidence_health ?? {}) as Partial<EvidenceHealthPass>) };
  const opencrqIds = integrations.filter((i) => i.provider === source.provider && i.status === "connected").map((i) => i.id);
  // The integration the evidence health pass reads, as `resolve_pass` picks it: the declared one, else the single connected one (#1937).
  const evidenceRuns = evidenceHealthRuns(evidenceHealth, opencrqIds).integration || evidenceHealth.integration.trim();
  const evidenceIds = evidenceRuns ? [evidenceRuns] : [];
  // The attack surface switch is the stored flag; the evidence health pass may run unset (#1344): the API's truth.
  const passOn = passField === "attack_surface" ? attackSurface.enabled : Boolean(source.available);
  const passIds = passField === "attack_surface" ? passIntegrationIds(attackSurface) : passField === "evidence_health" ? evidenceIds : [];
  const runsOn = passIds
    .map((id) => integrations.find((i) => i.id === id))
    .filter((i): i is NonNullable<typeof i> => Boolean(i) && i?.provider === source.provider)
    .map((i) => i.name);
  const noScope = passField === "attack_surface" && !(profile?.owned_domains?.length || profile?.address_ranges?.length);
  // A pass switch needs the integration it runs on: chosen on its card, or the single connected one; without one it points there.
  const switchable = (passField === "evidence_health" || passField === "attack_surface") && passIds.length > 0;

  const togglePass = (enabled: boolean) => {
    if (passField === "attack_surface") onSave({ attack_surface: { ...attackSurface, enabled } }, t_i18n("Scheduled pass saved"));
    // An off names the integration it ran on, as the Settings card does: `enabled: false` with none declared is the
    // legacy stored default, which `resolve_pass` reads as on, so the off would not hold (#1937).
    else if (passField === "evidence_health")
      onSave(
        { evidence_health: { ...evidenceHealth, enabled, integration: evidenceHealthSavedIntegration(evidenceHealth.integration.trim(), enabled, opencrqIds) } },
        t_i18n("Scheduled pass saved"),
      );
  };
  const toggleUse = (use: boolean) =>
    onSave(
      { signal_sources: withExclusion(profile?.signal_sources, path.id, key, !use, source.default_on !== false) },
      use ? t_i18n("Source used on this path") : t_i18n("Source taken off this path"),
    );

  // The row's header (name, kind, state, counts) is the table's: this is the source's configuration, its opened row.
  return (
    <div
      className="flex flex-col gap-2"
      data-testid="ctem-path-source"
      data-provider={source.provider}
      data-kind={source.kind}
      data-state={state}
    >
      {source.event && <p className="content-compact text-default-secondary">{t_i18n(source.event)}</p>}
      {source.kind === "pir_playbook" && state !== "not_connected" && (
        <PirSelection path={path} profile={profile} isAdmin={isAdmin} saving={saving} onSave={onSave} />
      )}

      {passField && state !== "not_connected" && (
        <div className="flex flex-col gap-1.5 content-compact">
          <label className="flex items-center justify-between gap-3">
            <span>{t_i18n("Scheduled pass on")}</span>
            <Switch
              checked={passOn}
              disabled={!isAdmin || !switchable || saving}
              onCheckedChange={togglePass}
              aria-label={t_i18n("Scheduled pass on")}
              data-testid="ctem-path-source-pass"
            />
          </label>
          <span className="text-default-secondary" data-testid="ctem-path-source-runs-on">
            {t_i18n("Runs on: {names}", { names: runsOn.length ? runsOn.join(", ") : t_i18n("None") })}
          </span>
          {(!switchable || noScope) && (
            <span className="flex items-center gap-1.5 text-default-secondary" data-testid="ctem-path-source-missing">
              <Icon name="info" size={16} aria-hidden />
              {!switchable ? t_i18n("Choose the integration it runs on in its settings card.") : t_i18n("No owned domain or address range is set: the pass has nothing to look up.")}
            </span>
          )}
          {shared.length > 0 && (
            <span className="text-default-secondary" data-testid="ctem-path-source-shared">
              {t_i18n("The switch is organization-wide. Also feeds: {paths}", { paths: shared.join(", ") })}
            </span>
          )}
          {isAdmin && (
            <span>
              <Button variant="ghost" size="sm" onClick={() => navigate(connectHref(source))} data-testid="ctem-path-source-settings">
                {t_i18n("Open its settings")}
              </Button>
            </span>
          )}
        </div>
      )}

      {source.kind === "listener" && !provisioned && state !== "not_connected" && (
        <p className="content-compact text-default-secondary">{t_i18n("A live listener runs while its integration is connected.")}</p>
      )}
      {provisioned && state === "error" && <SourceFixLinks source={source} platformSources={platformSources} />}
      {provisioned && state !== "not_connected" && (
        <PlatformSourceControls source={source} platformSources={platformSources} isAdmin={isAdmin} onEnable={onEnable} />
      )}
      {provisioned && (state === "not_set_up" || state === "error") && isAdmin && firstOff && (
        <span>
          <Button variant="secondary" size="sm" onClick={() => onEnable(firstOff)} data-testid="ctem-path-source-set-up">
            {t_i18n("Set up")}
          </Button>
        </span>
      )}

      {canExclude(source) && (
        <label className="flex items-center justify-between gap-3 content-compact">
          <span>{t_i18n("Use on this path")}</span>
          <Switch
            checked={!source.excluded}
            disabled={!isAdmin || saving}
            onCheckedChange={toggleUse}
            aria-label={t_i18n("Use on this path")}
            data-testid="ctem-path-source-use"
          />
        </label>
      )}

      {/* A profile pass or the PIR playbook is connected from the organization profile: no Connect for a reader (UX r1 #1461 B1). */}
      {state === "not_connected" && (isAdmin || !connectNeedsAdmin(source)) && (
        <span>
          <Button variant="secondary" size="sm" onClick={() => navigate(connectHref(source))} data-testid="ctem-path-source-connect">
            {t_i18n("Connect")}
          </Button>
        </span>
      )}
    </div>
  );
}

interface SourceTableRow {
  key: string;
  source: CtemSignalPathSource;
  state: SourceState;
  name: string;
  /** The integrations it runs on, by name (a source XTM One provisions), joined. */
  integration: string;
  /** The same integrations, one name each: the cell shows the first and +N (UX round 1 on #2741, B4). */
  integrations: string[];
  /** The run-as user ids of its enabled platform lines, joined (a source XTM One provisions). */
  runAs: string;
}

/** The sort order of the states: what needs a look first. */
const STATE_RANK: Record<SourceState, number> = { error: 0, bound: 1, off: 2, not_configured: 3, not_set_up: 3, excluded: 4, not_connected: 5 };

function stateLabel(state: SourceState): string {
  return sourceStateIcon(state)?.label ?? "Not connected";
}

function toggleSet(prev: ReadonlySet<string>, key: string): ReadonlySet<string> {
  const next = new Set(prev);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return next;
}

function ownLines(source: CtemSignalPathSource, platform: readonly CtemPlatformSource[]): CtemPlatformSource[] {
  return platform.filter((s) => s.provider === source.provider && s.kind === source.kind);
}

function runAsUsers(source: CtemSignalPathSource, platform: readonly CtemPlatformSource[]): string {
  return [...new Set(ownLines(source, platform).filter((s) => s.enabled && s.run_as_user_id).map((s) => s.run_as_user_id as string))].join(",");
}

/**
 * The reason lines of a row, one per integration and named (UX round 1 on #2653, B1): each reason ends with its
 * full stop, then its fix in the reader's language and the last attempt (B2). An error reads the path answer
 * (#2637), so a reader without the platform list reads why too; a Not configured row its platform lines.
 */
function useReasonLines(row: SourceTableRow, platformSources: readonly CtemPlatformSource[]): string[] {
  const { t_i18n, intl } = useFormatter();
  const formatTime = (iso: string) => intl.formatDate(iso, { dateStyle: "medium", timeStyle: "short" });
  const own = ownLines(row.source, platformSources);
  const names = new Map(own.map((s) => [s.integration_id, s.integration_name]));
  if (row.source.last_error) return [row.source.last_error];
  const fromPath = row.state === "error" ? sourceErrorLines(row.source, names, t_i18n, formatTime) : [];
  if (fromPath.length > 0) return fromPath;
  return own.map((s) => sourceReasonLine(s, s.integration_name, t_i18n)).filter(Boolean);
}

/** The status of a row: the one state word of the path list (A4), with its reasons in its tooltip, one line each. */
function SourceStatus({ row, platformSources }: { row: SourceTableRow; platformSources: readonly CtemPlatformSource[] }) {
  const { t_i18n } = useFormatter();
  const icon = sourceStateIcon(row.state);
  const lines = useReasonLines(row, platformSources);
  // A one-word status wraps rather than being cut (UX round 1 on #2653, B3).
  const word = (
    <span className="flex min-w-0 items-center gap-1" data-testid="ctem-path-source-state">
      {icon && <Icon name={icon.icon} size={16} className={cn("shrink-0", icon.className)} aria-hidden />}
      <span className="min-w-0 break-words">{t_i18n(stateLabel(row.state))}</span>
    </span>
  );
  if (lines.length === 0) return word;
  const content = (
    <span className="flex flex-col gap-1" data-testid="ctem-path-source-reasons">
      {lines.map((line) => (
        <span key={line}>{line}</span>
      ))}
    </span>
  );
  return <Tip content={content}>{word}</Tip>;
}

/**
 * The fix of each integration in error, in the opened row (UX round 1 on #2653, B2): a real action, the page
 * where it is done; a fix done on this page (its switch, Set up) is the row's own control and gets no link.
 */
function SourceFixLinks({ source, platformSources }: { source: CtemSignalPathSource; platformSources: readonly CtemPlatformSource[] }) {
  const { t_i18n } = useFormatter();
  const names = new Map(ownLines(source, platformSources).map((s) => [s.integration_id, s.integration_name]));
  const fixes = (source.errors ?? [])
    .map((e) => ({ e, route: platformSourceFixRoute(e) }))
    .filter((f): f is { e: NonNullable<CtemSignalPathSource["errors"]>[number]; route: string } => Boolean(f.route));
  if (fixes.length === 0) return null;
  return (
    <ul className="flex flex-col gap-1 content-compact" data-testid="ctem-path-source-fixes">
      {fixes.map(({ e, route }) => (
        <li key={e.integration_id} className="flex min-w-0 items-center gap-2">
          <span className="min-w-0 text-default-secondary">
            {sourceReasonLine(e, names.get(e.integration_id), t_i18n)}
          </span>
          <Link to={route} className="shrink-0 text-accent underline-offset-2 hover:underline" data-testid="ctem-path-source-fix">
            {t_i18n("Fix it")}
          </Link>
        </li>
      ))}
    </ul>
  );
}

/** The side panel of one trigger path's sources; `path` null closes it. */
export function SignalPathSourcesSheet({
  path,
  paths,
  connected,
  onClose,
}: {
  path: CtemSignalPath | null;
  paths: readonly CtemSignalPath[];
  connected: ReadonlySet<string>;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const { data } = useOrganizationContext();
  const save = usePatchOrganizationContext();
  const qc = useQueryClient();
  const profile = data?.profile;
  const labels = Object.fromEntries(paths.map((p) => [p.id, t_i18n(p.label)]));
  // The platform sources are an administrator's read (#2434): a reader reads the path states alone.
  const { data: platform } = useCtemPlatformSources(isAdmin && !!path);
  const platformSources = platform?.sources ?? [];
  const [enabling, setEnabling] = useState<CtemPlatformSource | null>(null);
  // The webhook sources (#2506): an administrator's read, for the configure dialog and the switch.
  const { data: hooks } = useCtemWebhookSources(isAdmin && !!path);
  const webhookById = new Map((hooks?.sources ?? []).map((w) => [w.id, w]));
  const updateHook = useUpdateCtemWebhookSource();
  const [hookDialog, setHookDialog] = useState<{ source: CtemWebhookSource | null } | null>(null);
  const [opened, setOpened] = useState<ReadonlySet<string>>(new Set());
  const { count: formatCount } = useNumberFormat();
  const { intl } = useFormatter();
  // The integrations a listener XTM One does not provision runs on (GitHub, Aikido), named on its row (#2736).
  const { data: integrations = [] } = useIntegrations();

  const onSave = (next: Partial<OrganizationProfile>, done: string) => {
    if (!profile || !isAdmin) return;
    save.mutate(
      // #1752: the changed fields alone travel, never the asset rows.
      { fields: next, based_on_version: data?.version ?? null },
      {
        onSuccess: () => {
          // The paths read the saved profile: status, sources and the connected count follow at once.
          void qc.invalidateQueries({ queryKey: ["ctem-signals", "paths"] });
          toast({ title: done, variant: "success" });
        },
        onError: (err) => toast({ title: t_i18n("The organization profile was not saved"), description: err.message, variant: "error" }),
      },
    );
  };

  const toggleHook = (id: string, enabled: boolean) =>
    updateHook.mutate(
      { id, enabled },
      {
        onSuccess: () => toast({ title: enabled ? t_i18n("Webhook source on") : t_i18n("Webhook source off"), variant: "success" }),
        onError: (err) => toast({ title: t_i18n("The webhook source was not saved"), description: err.message, variant: "error" }),
      },
    );

  const status = path ? pathStatus(path) : null;
  // Every source of the path, a provider with no integration here included: the detail is where it is found (#2506).
  const rows: SourceTableRow[] = (path?.sources ?? []).map((source) => ({
    key: sourceKey(source),
    source,
    state: sourceDisplayState(source, connected, platformSources),
    // The name the list chip shows, so both say the same (UX advisory A1 on #2741); the kind word is its own column.
    name: sourceShortName(source, path?.id ?? "", providerName, t_i18n),
    integration: sourceIntegrationNames(source, platformSources, integrations),
    integrations: sourceIntegrationList(source, platformSources, integrations),
    runAs: runAsUsers(source, platformSources),
  }));
  const columns: DataTableColumn<SourceTableRow>[] = [
    {
      key: "name",
      header: "Source",
      sortValue: (row) => row.name,
      csv: (row) => row.name,
      cell: (row) => (
        <span className="flex min-w-0 items-center gap-2">
          <ProviderLogo provider={row.source.provider} className="size-4 shrink-0" decorative />
          {/* The full name in the DS tooltip when the cell cuts it (UX round 1 on #2509, B2). */}
          <Tip content={row.name}>
            <span className="min-w-0 truncate font-medium" data-testid="ctem-path-source-name">
              {row.name}
            </span>
          </Tip>
        </span>
      ),
    },
    {
      key: "kind",
      header: "Kind",
      hideable: true,
      // Fixed widths on every column but Source, which takes the free width (UX round 1 on #2509, B2).
      width: 128,
      // The kind word alone, whole (UX round 1 on #2741, B4): what tells two rows apart is in the Source name.
      sortValue: (row) => t_i18n(SOURCE_KIND_LABEL[row.source.kind]),
      csv: (row) => sourceKindText(t_i18n, row.source),
      cell: (row) => (
        <span className="break-words text-default-secondary" data-testid="ctem-path-source-kind">
          {t_i18n(SOURCE_KIND_LABEL[row.source.kind])}
        </span>
      ),
    },
    {
      key: "integration",
      header: "Integration",
      hideable: true,
      hideBelow: "4xl",
      width: 144,
      sortValue: (row) => row.integration,
      csv: (row) => row.integration,
      // "No integration" in words, never "None" beside a status (#2736).
      // A list in a narrow cell: the first name and +N, the whole list in the DS tooltip, never a comma run cut
      // (UX round 1 on #2741, B4).
      cell: (row) =>
        row.integrations.length > 0 ? (
          <Tip content={row.integration}>
            <span className="flex min-w-0 items-center gap-1" data-testid="ctem-path-source-integration">
              <span className="min-w-0 truncate" data-testid="ctem-path-source-integration-first">{row.integrations[0]}</span>
              {row.integrations.length > 1 && (
                <span className="shrink-0 tabular-nums text-default-secondary" data-testid="ctem-path-source-integration-more">
                  {t_i18n("+{count}", { count: row.integrations.length - 1 })}
                </span>
              )}
            </span>
          </Tip>
        ) : (
          <span className="truncate text-default-secondary" data-testid="ctem-path-source-integration">
            {row.source.kind === "webhook" ? t_i18n("Its own receiver") : t_i18n("No integration")}
          </span>
        ),
    },
    {
      key: "run_as",
      header: "Run as",
      hideable: true,
      hideBelow: "6xl",
      width: 128,
      sortValue: (row) => row.runAs,
      csv: (row) => row.runAs,
      cell: (row) => (row.runAs ? <RunAsName ids={row.runAs} /> : <span className="text-default-secondary">{t_i18n("None")}</span>),
    },
    {
      key: "status",
      header: "Status",
      // Room for the longest state word in en/fr/es/ja; a longer one wraps, never cut (UX round 1 on #2653, B3).
      width: 160,
      sortValue: (row) => STATE_RANK[row.state],
      csv: (row) => t_i18n(stateLabel(row.state)),
      cell: (row) => <SourceStatus row={row} platformSources={platformSources} />,
    },
    {
      key: "count_30d",
      header: "Signals (30 days)",
      align: "right",
      // Wide enough for the header and its sort mark (UX A3 of #2436).
      width: 168,
      hideable: true,
      firstSort: "desc",
      sortValue: (row) => row.source.count_30d ?? 0,
      csv: (row) => row.source.count_30d ?? 0,
      cell: (row) => <span className="tabular-nums">{formatCount(row.source.count_30d ?? 0)}</span>,
    },
    {
      key: "last_signal_at",
      header: "Last signal",
      hideable: true,
      hideBelow: "4xl",
      width: 128,
      sortValue: (row) => row.source.last_signal_at ?? "",
      csv: (row) => row.source.last_signal_at ?? "",
      cell: (row) =>
        row.source.last_signal_at ? (
          <span className="tabular-nums">{formatShortDateTimeIntl(intl, row.source.last_signal_at)}</span>
        ) : (
          <span className="text-default-secondary">{t_i18n("None")}</span>
        ),
    },
  ];
  const filters: DataTableFilter<SourceTableRow>[] = [
    {
      key: "provider",
      label: "Provider",
      values: (row) => [row.source.provider],
      options: [...new Set(rows.map((r) => r.source.provider))].map((p) => ({ value: p, label: providerName(p) })),
    },
    {
      key: "status",
      label: "Status",
      values: (row) => [row.state],
      options: [...new Set(rows.map((r) => r.state))].map((s) => ({ value: s, label: t_i18n(stateLabel(s)) })),
    },
  ];
  const rowActions = (row: SourceTableRow) => {
    const hook = row.source.webhook_id ? webhookById.get(row.source.webhook_id) : undefined;
    return (
      <span className="inline-flex items-center gap-1">
        {hook && isAdmin && (
          <Switch
            checked={hook.enabled}
            disabled={updateHook.isPending}
            onCheckedChange={(on) => toggleHook(hook.id, on)}
            aria-label={hook.enabled ? t_i18n("Disable") : t_i18n("Enable")}
            data-testid="ctem-path-source-webhook-switch"
          />
        )}
        {hook && isAdmin && (
          <Tip content={t_i18n("Configure and test")}>
            <Button variant="ghost" size="sm" onClick={() => setHookDialog({ source: hook })} data-testid="ctem-path-source-webhook-configure">
              <Icon name="chevron-right" size={16} aria-hidden />
              <span className="sr-only">{t_i18n("Configure and test")}</span>
            </Button>
          </Tip>
        )}
        {!(hook && isAdmin) && (
          // A chevron says the row expands (#2736, Sam 28 Nov 2026): never a gear; the whole row toggles it too.
          <Tip content={opened.has(row.key) ? t_i18n("Close its settings") : t_i18n("Configure")}>
            <Button
              variant="ghost"
              size="sm"
              aria-expanded={opened.has(row.key)}
              onClick={() => setOpened((prev) => toggleSet(prev, row.key))}
              data-testid="ctem-path-source-configure"
            >
              <Icon name="chevron-down" size={16} className={cn("transition-transform", opened.has(row.key) && "rotate-180")} aria-hidden />
              <span className="sr-only">{t_i18n("Configure")}</span>
            </Button>
          </Tip>
        )}
      </span>
    );
  };

  return (
    <SlideOver open={!!path} onClose={onClose} label={t_i18n("Sources of this trigger path")} width="w-[min(1080px,80vw)]">
      {path && status && (
        <div className="flex h-full min-h-0 flex-col" data-testid="ctem-path-sources" data-path={path.id}>
          <div className="flex items-start justify-between gap-3 border-b border-elevation-subtle p-4">
            <div className="flex min-w-0 items-start gap-2.5">
              <span className="flex size-7 shrink-0 items-center justify-center rounded-sm bg-elevation-hover text-accent" aria-hidden>
                <Icon name={(PATH_ICON[path.id] ?? "activity") as IconName} size={16} />
              </span>
              <div className="flex min-w-0 flex-col gap-1">
                <h2 className="title-sm">{t_i18n(path.label)}</h2>
                {path.description && <p className="content-compact text-default-secondary">{t_i18n(path.description)}</p>}
                <span>
                  <Chip label={pathStatusLabel(path, t_i18n)} severity={PATH_STATUS_TONE[status]} data-testid="ctem-path-sources-status" />
                </span>
              </div>
            </div>
            {/* The panel's own action sits in its header beside Close, out of the table toolbar (UX round 1 on #2509, B4). */}
            <span className="flex shrink-0 items-center gap-2">
              {isAdmin && (
                <Button variant="secondary" size="sm" onClick={() => setHookDialog({ source: null })} data-testid="ctem-path-source-add-webhook">
                  <Icon name="plus" size={16} aria-hidden />
                  {t_i18n("Add a signed webhook")}
                </Button>
              )}
              <Button variant="ghost" size="sm" onClick={onClose} data-testid="ctem-path-sources-close">
                <Icon name="x" size={16} aria-hidden />
                {t_i18n("Close")}
              </Button>
            </span>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-4" data-testid="ctem-path-sources-scroll">
            {!isAdmin && (
              <p className="pt-3 content-compact text-default-secondary" data-testid="ctem-path-sources-read-only">
                {t_i18n("Only an administrator can change the sources of a trigger path.")}
              </p>
            )}
            <div className="py-3">
              <DataTable
                rows={rows}
                columns={columns}
                rowKey={(row) => row.key}
                defaultSort={{ key: "status", direction: "asc" }}
                searchFields={(row) => [row.name, row.integration, sourceKindText(t_i18n, row.source)]}
                searchPlaceholder={t_i18n("Search the sources of this path")}
                filters={filters}
                defaultPageSize={10}
                compactSearch
                layout="fixed"
                rowTestId="ctem-path-source-row"
                rowDataAttributes={(row) => ({ "data-state": row.state, "data-provider": row.source.provider, "data-kind": row.source.kind })}
                rowActions={rowActions}
                rowActionsWidth={isAdmin ? 104 : 56}
                // Every row opens on a click anywhere (#2736): a webhook row its configure view, any other its settings in place.
                onRowClick={(row) => {
                  const hook = row.source.webhook_id ? webhookById.get(row.source.webhook_id) : undefined;
                  if (hook && isAdmin) setHookDialog({ source: hook });
                  else setOpened((prev) => toggleSet(prev, row.key));
                }}
                toggleInCell
                expandedRow={(row) =>
                  opened.has(row.key) ? (
                    <SourceConfig
                      path={path}
                      source={row.source}
                      connected={connected}
                      labels={labels}
                      profile={profile}
                      isAdmin={isAdmin}
                      saving={save.isPending}
                      onSave={onSave}
                      platformSources={platformSources}
                      onEnable={setEnabling}
                    />
                  ) : null
                }
                emptyLabel={t_i18n("Agents and the API record this path: it has no integration source.")}
                noResultsLabel={t_i18n("No source matches the search and filters.")}
              />
            </div>
          </div>
        </div>
      )}
      {path && (
        <WebhookSourceDialog
          open={hookDialog !== null}
          pathId={path.id}
          pathLabel={t_i18n(path.label)}
          source={hookDialog?.source ?? null}
          onClose={() => setHookDialog(null)}
        />
      )}
      <PlatformSourceEnableDialog
        source={enabling}
        label={enabling ? `${providerName(enabling.provider)} ${t_i18n(SOURCE_KIND_LABEL[enabling.kind])}` : ""}
        onClose={() => setEnabling(null)}
      />
    </SlideOver>
  );
}
