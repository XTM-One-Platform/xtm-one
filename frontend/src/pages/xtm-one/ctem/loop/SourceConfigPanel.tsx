// SPDX-License-Identifier: Apache-2.0

/**
 * The configuration of one source, drawn in its opened row of the path table (#2740, dev-docs/ctem-signals.md
 * section 2bis). One skeleton for every source kind, nothing lost from the former stacked lines:
 * - a header: what the source listens to and, for a source XTM One provisions, one muted line on what it provisions;
 * - Integrations: one aligned row per integration (state, run-as, Open in actions, labelled switch), the scheduled
 *   pass's runs-on, schedule and organization-wide switch, or Connect;
 * - PIRs, on the PIR playbook only;
 * - Objects in the platform, summarized by kind and collapsed past three, the fleets' objects apart;
 * - Paths: the paths it feeds as chips (this one marked) and "Use on this path" as a labelled switch.
 */
import type { ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Chip, Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import type { CtemPlatformSource, CtemSignalPath, CtemSignalPathSource } from "@/hooks/api/useCtemSignalsApi";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import type { OrganizationProfile } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import {
  DEFAULT_ATTACK_SURFACE,
  DEFAULT_EVIDENCE_HEALTH,
  type EvidenceHealthPass,
  evidenceHealthRuns,
  evidenceHealthSavedIntegration,
  passIntegrationIds,
} from "@/lib/organization-profile";
import { LabelledSwitch } from "./LabelledSwitch";
import { PlatformSourceControls, PlatformSourceObjects } from "./PlatformSourceControls";
import { PLATFORM_NAME, isProvisioned, platformSourceFixRoute, sourceReasonLine } from "./platform-sources";
import { PirSelection } from "./PirSelection";
import { canExclude, connectHref, connectNeedsAdmin, profilePassField, sourceKey, sourceState, withExclusion } from "./signal-sources";

/** One titled section of the panel: a label column on the left, its content on the right, on one grid. */
function ConfigSection({ title, testId, children }: { title: string; testId: string; children: ReactNode }) {
  return (
    <section
      className="grid grid-cols-[minmax(0,9rem)_minmax(0,1fr)] gap-x-4 border-t border-elevation-subtle-soft py-2.5 content-compact"
      data-testid={testId}
      aria-label={title}
    >
      <h3 className="pt-1.5 font-medium">{title}</h3>
      <div className="flex min-w-0 flex-col gap-1.5">{children}</div>
    </section>
  );
}

function ownLines(source: CtemSignalPathSource, platform: readonly CtemPlatformSource[]): CtemPlatformSource[] {
  return platform.filter((s) => s.provider === source.provider && s.kind === source.kind);
}

/**
 * The fix of each integration in error (UX round 1 on #2653, B2): a real action, the page where it is done; a fix
 * done on this page (its switch, Set up) is the row's own control and gets no link.
 */
function SourceFixLinks({ source, platformSources }: { source: CtemSignalPathSource; platformSources: readonly CtemPlatformSource[] }) {
  const { t_i18n } = useFormatter();
  const names = new Map(ownLines(source, platformSources).map((s) => [s.integration_id, s.integration_name]));
  const fixes = (source.errors ?? [])
    .map((e) => ({ e, route: platformSourceFixRoute(e) }))
    .filter((f): f is { e: NonNullable<CtemSignalPathSource["errors"]>[number]; route: string } => Boolean(f.route));
  if (fixes.length === 0) return null;
  return (
    <ul className="flex flex-col gap-1" data-testid="ctem-path-source-fixes">
      {fixes.map(({ e, route }) => (
        <li key={e.integration_id} className="flex min-w-0 items-center gap-2">
          <span className="min-w-0 text-default-secondary">{sourceReasonLine(e, names.get(e.integration_id), t_i18n)}</span>
          <Button asChild variant="secondary" size="sm" className="shrink-0">
            <Link to={route} data-testid="ctem-path-source-fix">
              {t_i18n("Fix it")}
            </Link>
          </Button>
        </li>
      ))}
    </ul>
  );
}

/**
 * The paths one switch of the source feeds, as chips, this path marked first (#2740). A chip carries its path label
 * alone and sizes to it (UX round 1 B1: never a cut chip); the current path is marked by the info tone and a leading
 * check, with aria-current and a screen-reader "This path".
 */
function FeedChips({ pathId, feeds }: { pathId: string; feeds: readonly { path: string; label: string }[] }) {
  const { t_i18n } = useFormatter();
  const ordered = [...feeds].sort((a, b) => Number(b.path === pathId) - Number(a.path === pathId));
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-1.5" data-testid="ctem-path-source-feeds">
      {ordered.map((f) => {
        const current = f.path === pathId;
        return (
          <span
            key={f.path}
            className="inline-flex shrink-0 items-center gap-1"
            data-testid="ctem-path-source-feed"
            data-current={current}
            aria-current={current ? "true" : undefined}
          >
            {current && <Icon name="check" size={16} className="text-feedback-info-primary" aria-hidden />}
            <Chip label={f.label} severity={current ? "info" : "neutral"} className="max-w-none shrink-0 whitespace-nowrap" />
            {current && <span className="sr-only">{t_i18n("This path")}</span>}
          </span>
        );
      })}
    </span>
  );
}

export function SourceConfig({
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
  const provisioned = isProvisioned(source);
  const own = ownLines(source, platformSources);
  // "Set up" opens the enable dialog of the first integration of the provider still off (#2434).
  const firstOff = own.find((s) => !s.enabled && s.integration_connected);

  const attackSurface = { ...DEFAULT_ATTACK_SURFACE, ...(profile?.attack_surface ?? {}) };
  const evidenceHealth: EvidenceHealthPass = { ...DEFAULT_EVIDENCE_HEALTH, ...((profile?.evidence_health ?? {}) as Partial<EvidenceHealthPass>) };
  const opencrqIds = integrations.filter((i) => i.provider === source.provider && i.status === "connected").map((i) => i.id);
  // The integration the evidence health pass reads, as `resolve_pass` picks it: the declared one, else the single connected one (#1937).
  const evidenceRuns = evidenceHealthRuns(evidenceHealth, opencrqIds).integration || evidenceHealth.integration.trim();
  const evidenceIds = evidenceRuns ? [evidenceRuns] : [];
  // The attack surface switch is the stored flag; the evidence health pass may run unset (#1344): the API's truth.
  const passOn = passField === "attack_surface" ? attackSurface.enabled : Boolean(source.available);
  const passIds = passField === "attack_surface" ? passIntegrationIds(attackSurface) : passField === "evidence_health" ? evidenceIds : [];
  const cadence = passField === "attack_surface" ? attackSurface.cadence_hours : passField === "evidence_health" ? evidenceHealth.cadence_hours : null;
  const runsOn = passIds
    .map((id) => integrations.find((i) => i.id === id))
    .filter((i): i is NonNullable<typeof i> => Boolean(i) && i?.provider === source.provider)
    .map((i) => i.name);
  const noScope = passField === "attack_surface" && !(profile?.owned_domains?.length || profile?.address_ranges?.length);
  // A pass switch needs the integration it runs on: chosen on its card, or the single connected one; without one it points there.
  const switchable = (passField === "evidence_health" || passField === "attack_surface") && passIds.length > 0;

  // The paths one switch stops: a provisioned source's own feeds, a pass's path and the paths it also feeds (#2740).
  const fromLines = own.flatMap((s) => s.feeds ?? []);
  const feeds = [
    ...new Map(
      (fromLines.length > 0 ? fromLines : [{ path: path.id, label: labels[path.id] ?? path.label }, ...(source.also_feeds ?? []).map((id) => ({ path: id, label: id }))]).map(
        (f) => [f.path, { path: f.path, label: labels[f.path] ?? t_i18n(f.label) }],
      ),
    ).values(),
  ];
  const platform = PLATFORM_NAME[source.provider] ?? source.provider;
  const showObjects = provisioned && state !== "not_connected" && own.some((s) => (s.objects ?? []).length > 0 || (s.fleet_objects ?? []).length > 0);
  const showPaths = feeds.length > 1 || canExclude(source);
  // A section renders only with content (UX round 1 B2): a source XTM One runs itself (the inventory pass) has no
  // integration row, so it gets no empty Integrations title.
  const showIntegrations = provisioned || Boolean(passField) || state === "not_connected";

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
    <div className="flex flex-col" data-testid="ctem-path-source" data-provider={source.provider} data-kind={source.kind} data-state={state}>
      <div className="flex flex-col gap-0.5 pb-2.5 content-compact" data-testid="ctem-path-source-header">
        {source.event && <p>{t_i18n(source.event)}</p>}
        {provisioned && state !== "not_connected" && (
          <p className="text-default-secondary">
            {t_i18n("XTM One provisions this source in its platform when you turn it on, as the run-as user you pick. Fleets and agents never turn it off.")}
          </p>
        )}
        {source.kind === "listener" && !provisioned && state !== "not_connected" && (
          <p className="text-default-secondary">{t_i18n("A live listener runs while its integration is connected.")}</p>
        )}
      </div>

      {showIntegrations && (
      <ConfigSection title={t_i18n("Integrations")} testId="ctem-path-source-integrations">
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
        {passField && state !== "not_connected" && (
          <>
            <LabelledSwitch
              label={t_i18n("Scheduled pass on")}
              note={feeds.length > 1 ? t_i18n("The switch is organization-wide.") : undefined}
              checked={passOn}
              disabled={!isAdmin || !switchable || saving}
              onCheckedChange={togglePass}
              testId="ctem-path-source-pass"
            />
            <dl className="grid grid-cols-[minmax(0,8rem)_minmax(0,1fr)] items-center gap-x-3 gap-y-1">
              <dt className="text-default-secondary">{t_i18n("Runs on")}</dt>
              <dd className="min-w-0 truncate" data-testid="ctem-path-source-runs-on">
                {runsOn.length ? runsOn.join(", ") : t_i18n("None")}
              </dd>
              {cadence != null && (
                <>
                  <dt className="text-default-secondary">{t_i18n("Schedule")}</dt>
                  <dd data-testid="ctem-path-source-schedule">{t_i18n("Every {count, plural, one {# hour} other {# hours}}", { count: cadence })}</dd>
                </>
              )}
            </dl>
            {(!switchable || noScope) && (
              <span className="flex items-center gap-1.5 text-default-secondary" data-testid="ctem-path-source-missing">
                <Icon name="info" size={16} aria-hidden />
                {!switchable ? t_i18n("Choose the integration it runs on in its settings card.") : t_i18n("No owned domain or address range is set: the pass has nothing to look up.")}
              </span>
            )}
            {isAdmin && (
              <span>
                <Button variant="secondary" size="sm" onClick={() => navigate(connectHref(source))} data-testid="ctem-path-source-settings">
                  <Icon name="settings" size={16} aria-hidden />
                  {t_i18n("Open its settings")}
                </Button>
              </span>
            )}
          </>
        )}
        {state === "not_connected" && (
          <span className="text-default-secondary" data-testid="ctem-path-source-not-connected">
            {t_i18n("No integration of this provider is connected.")}
          </span>
        )}
        {/* A profile pass or the PIR playbook is connected from the organization profile: no Connect for a reader (UX r1 #1461 B1). */}
        {state === "not_connected" && (isAdmin || !connectNeedsAdmin(source)) && (
          <span>
            <Button variant="secondary" size="sm" onClick={() => navigate(connectHref(source))} data-testid="ctem-path-source-connect">
              {t_i18n("Connect")}
            </Button>
          </span>
        )}
      </ConfigSection>
      )}

      {source.kind === "pir_playbook" && state !== "not_connected" && (
        <ConfigSection title={t_i18n("PIRs")} testId="ctem-path-source-pir-section">
          <PirSelection path={path} profile={profile} isAdmin={isAdmin} saving={saving} onSave={onSave} />
        </ConfigSection>
      )}

      {showObjects && (
        <ConfigSection title={t_i18n("Objects in {platform}", { platform })} testId="ctem-path-source-objects">
          <PlatformSourceObjects source={source} platformSources={platformSources} />
        </ConfigSection>
      )}

      {showPaths && (
        <ConfigSection title={t_i18n("Paths")} testId="ctem-path-source-paths">
          {feeds.length > 1 && (
            <span className="flex min-w-0 flex-col gap-1" data-testid="ctem-path-source-shared">
              <FeedChips pathId={path.id} feeds={feeds} />
              <span className="text-default-secondary">{t_i18n("Turning it off stops all of them.")}</span>
            </span>
          )}
          {canExclude(source) && (
            <LabelledSwitch
              label={t_i18n("Use on this path")}
              note={feeds.length > 1 ? t_i18n("Taking it off this path keeps it on for the others.") : undefined}
              checked={!source.excluded}
              disabled={!isAdmin || saving}
              onCheckedChange={toggleUse}
              testId="ctem-path-source-use"
            />
          )}
        </ConfigSection>
      )}
    </div>
  );
}
