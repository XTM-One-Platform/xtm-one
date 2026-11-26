// SPDX-License-Identifier: Apache-2.0

/**
 * The sources of one trigger path, managed in place (#1445, dev-docs/ctem-signals.md section 2bis).
 *
 * Opened from a row of the signal sources page. Every source of the path is listed, connected or not, with
 * its state and the control that exists for it today:
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
 * - a provider with no integration here: Connect, the integration creation flow.
 *
 * Every write is an organization profile save, so administrators only: a reader sees the same states
 * without controls.
 */
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Chip, Icon, type IconName } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { SlideOver } from "@/components/ui/slide-over";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { type CtemPlatformSource, type CtemSignalPath, type CtemSignalPathSource, useCtemPlatformSources } from "@/hooks/api/useCtemSignalsApi";
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
import { PlatformSourceControls, PlatformSourceEnableDialog } from "./PlatformSourceControls";
import { isProvisioned } from "./platform-sources";
import { PirSelection } from "./PirSelection";
import {
  PATH_ICON,
  PATH_STATUS_TONE,
  SOURCE_KIND_LABEL,
  canExclude,
  connectHref,
  connectNeedsAdmin,
  pathStatus,
  pathStatusLabel,
  profilePassField,
  sourceKey,
  sourceState,
  sourceStateIcon,
  withExclusion,
} from "./signal-sources";

function providerName(provider: string): string {
  return CTEM_PRODUCT_INFO[provider]?.name ?? INTEGRATION_PROVIDER_NAMES[provider] ?? provider;
}

function SourceRow({
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
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const { data: integrations = [] } = useIntegrations();
  const state = sourceState(source, connected);
  const passField = profilePassField(source);
  const key = sourceKey(source);
  const shared = (source.also_feeds ?? []).map((id) => labels[id] ?? id);
  const provisioned = isProvisioned(source);
  // "Set up" opens the enable dialog of the first integration of the provider still off (#2434).
  const firstOff = platformSources.find((s) => s.provider === source.provider && s.kind === source.kind && !s.enabled && s.integration_connected);
  // The source's word and icon are the row chip's (#2436, Sam 26 Nov 2026: same wording in the side panel): On, Off,
  // Not used here, Not set up or Error; a source XTM One provisions reads its platform status (UX round 1 on #2442, B1).
  const own = platformSources.filter((s) => s.provider === source.provider && s.kind === source.kind);
  const shownState =
    provisioned && own.length > 0 && state !== "not_connected" && state !== "excluded"
      ? own.some((s) => s.status === "error")
        ? "error"
        : own.some((s) => s.status === "connected")
          ? "bound"
          : "off"
      : state;
  const icon = sourceStateIcon(shownState);
  const { count: formatCount } = useNumberFormat();

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

  return (
    <li
      className="flex flex-col gap-2 border-b border-elevation-subtle py-3 last:border-b-0"
      data-testid="ctem-path-source"
      data-provider={source.provider}
      data-kind={source.kind}
      data-state={state}
    >
      <div className="flex min-w-0 items-center gap-2">
        <ProviderLogo provider={source.provider} className={cn("size-4 shrink-0", state === "not_connected" && "opacity-50 grayscale")} decorative />
        <span className={cn("truncate font-medium", state === "not_connected" && "text-default-secondary")}>{providerName(source.provider)}</span>
        <span className="shrink-0 content-caption text-default-secondary">{t_i18n(SOURCE_KIND_LABEL[source.kind])}</span>
        <span className="ml-auto flex shrink-0 items-center gap-1 content-caption text-default-secondary">
          {icon && <Icon name={icon.icon} size={16} className={icon.className} aria-hidden data-testid="ctem-path-source-state-icon" />}
          <span data-testid="ctem-path-source-state">{icon ? t_i18n(icon.label) : t_i18n("Not connected on this platform")}</span>
        </span>
      </div>
      {source.event && <p className="content-compact text-default-secondary">{t_i18n(source.event)}</p>}
      {source.count_30d !== undefined && state !== "not_connected" && (
        <p className="content-compact text-default-secondary" data-testid="ctem-path-source-activity">
          {t_i18n("{count, plural, one {{shown} signal in 30 days} other {{shown} signals in 30 days}}", { count: source.count_30d, shown: formatCount(source.count_30d) })}
          {". "}
          {t_i18n("Last signal: {date}", { date: source.last_signal_at ? formatShortDateTimeIntl(intl, source.last_signal_at) : t_i18n("None") })}
        </p>
      )}
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
    </li>
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

  const status = path ? pathStatus(path) : null;
  return (
    <SlideOver open={!!path} onClose={onClose} label={t_i18n("Sources of this trigger path")} width="w-[min(520px,45vw)]">
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
            <Button variant="ghost" size="sm" onClick={onClose} data-testid="ctem-path-sources-close">
              <Icon name="x" size={16} aria-hidden />
              {t_i18n("Close")}
            </Button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-4" data-testid="ctem-path-sources-scroll">
            {!isAdmin && (
              <p className="pt-3 content-compact text-default-secondary" data-testid="ctem-path-sources-read-only">
                {t_i18n("Only an administrator can change the sources of a trigger path.")}
              </p>
            )}
            {(path.sources ?? []).length === 0 ? (
              <p className="py-3 content-compact text-default-secondary">{t_i18n("Agents and the API record this path: it has no integration source.")}</p>
            ) : (
              <ul>
                {(path.sources ?? []).map((source) => (
                  <SourceRow
                    key={sourceKey(source)}
                    path={path}
                    source={source}
                    connected={connected}
                    labels={labels}
                    profile={profile}
                    isAdmin={isAdmin}
                    saving={save.isPending}
                    onSave={onSave}
                    platformSources={platformSources}
                    onEnable={setEnabling}
                  />
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
      <PlatformSourceEnableDialog
        source={enabling}
        label={enabling ? `${providerName(enabling.provider)} ${t_i18n(SOURCE_KIND_LABEL[enabling.kind])}` : ""}
        onClose={() => setEnabling(null)}
      />
    </SlideOver>
  );
}
