// SPDX-License-Identifier: Apache-2.0

/**
 * CTEM > Inventory (with the outside lens card): "Evidence health" (#1344), the card the signal sources'
 * "Turn on" opens for the OpenCRQ evidence health pass (CTEM-28, trigger paths 9 and 12,
 * dev-docs/ctem-signals.md s.2bis). Read view at rest; Edit (administrators, like the PUT)
 * opens the switch, the integration and the two numbers; Save writes a new profile version
 * with this field alone changed. On by default: until an administrator saves it, the pass
 * runs on the single connected OpenCRQ integration.
 */

import { useId, useState } from "react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SECTION_BAR_ANCHOR_CLASS } from "@/components/ui/section-index";
import { SearchableSelect } from "@/components/ui/searchable-select";
import {
  SettingsCard,
  type SettingsCardFold,
  SettingsField,
  SettingsFieldGrid,
  SettingsSaveBar,
  SettingsSwitchField,
  settingsFieldHintId,
  settingsFieldLabelId,
} from "@/components/ui/settings-form";
import { SettingsReadField, SettingsReadGrid } from "@/components/ui/settings-read-view";
import { useToast } from "@/components/ui/toast";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { type OrganizationProfile, useOrganizationContext, useSaveOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import {
  boundedInteger,
  DEFAULT_EVIDENCE_HEALTH,
  EVIDENCE_HEALTH_BOUNDS,
  type EvidenceHealthPass,
  evidenceHealthRuns,
  evidenceHealthSavedIntegration,
  fromForm,
  toForm,
} from "@/lib/organization-profile";

/** The Select value of "no integration declared": the single connected one. */
const AUTO = "__auto__";

function CardIcon({ className }: { className?: string }) {
  return (
    <span className={className}>
      <Icon name="clipboard-check" size={16} />
    </span>
  );
}

interface Draft {
  enabled: boolean;
  integration: string;
  maxAge: string;
  cadence: string;
}

interface OrganizationEvidenceHealthCardProps {
  /** The landing anchor a `?card=evidence_health` link focuses (the signal sources' Turn on). */
  id: string;
  /** The page's fold props, like the sibling cards (read mode only). */
  fold?: SettingsCardFold;
}

export function OrganizationEvidenceHealthCard({ id: anchorId, fold }: OrganizationEvidenceHealthCardProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const id = useId();
  const { isAdmin } = useAuth();
  const { data } = useOrganizationContext();
  const save = useSaveOrganizationContext();
  const { data: integrations = [] } = useIntegrations();
  const [draft, setDraft] = useState<Draft | null>(null);

  const profile = data?.profile;
  const pass: EvidenceHealthPass = { ...DEFAULT_EVIDENCE_HEALTH, ...((profile?.evidence_health ?? {}) as Partial<EvidenceHealthPass>) };
  const candidates = integrations.filter((integration) => integration.provider === "opencrq" && integration.status === "connected");
  const nameOf = (integrationId: string) =>
    integrations.find((integration) => integration.id === integrationId)?.name ?? t_i18n("Integration not found");
  const state = evidenceHealthRuns(pass, candidates.map((integration) => integration.id));
  const declared = pass.integration.trim();

  const statusText = state.runs
    ? t_i18n("On, reading {name}", { name: nameOf(state.integration) })
    : !candidates.length
      ? t_i18n("No connected OpenCRQ integration")
      : pass.enabled === false && declared
        ? t_i18n("Off")
        : t_i18n("Off: pick the OpenCRQ integration to read");

  const start = () =>
    setDraft({
      enabled: pass.enabled ?? true,
      integration: declared || AUTO,
      maxAge: String(pass.max_age_days),
      cadence: String(pass.cadence_hours),
    });

  const maxAge = draft ? boundedInteger(draft.maxAge, EVIDENCE_HEALTH_BOUNDS.max_age_days) : null;
  const cadence = draft ? boundedInteger(draft.cadence, EVIDENCE_HEALTH_BOUNDS.cadence_hours) : null;
  // Several connected integrations and none picked: the pass cannot choose, so a pick is required to turn it on.
  const pickMissing = Boolean(draft?.enabled && draft.integration === AUTO && candidates.length > 1);
  // Off on the default setting names the single connected integration, so the off holds (UX round 1 on #1356, B4).
  const savedIntegration = (pick: string, enabled: boolean) =>
    evidenceHealthSavedIntegration(pick === AUTO ? "" : pick, enabled, candidates.map((integration) => integration.id));
  const next: EvidenceHealthPass | null =
    draft && maxAge !== null && cadence !== null
      ? { enabled: draft.enabled, integration: savedIntegration(draft.integration, draft.enabled), max_age_days: maxAge, cadence_hours: cadence }
      : null;
  const dirty = draft !== null && (next === null || JSON.stringify(next) !== JSON.stringify({ ...pass, enabled: pass.enabled ?? true }));
  const blocked = next === null || pickMissing;

  const onSave = () => {
    if (!profile || !next || blocked) return;
    const parsed = fromForm({ ...toForm(profile), evidence_health: { ...next } });
    if ("invalid" in parsed) return;
    save.mutate(
      { profile: parsed.profile as OrganizationProfile, source: "form" },
      {
        onSuccess: () => {
          setDraft(null);
          toast({ title: t_i18n("Organization profile saved"), variant: "success" });
        },
        onError: (err) => toast({ title: t_i18n("The organization profile was not saved"), description: err.message, variant: "error" }),
      },
    );
  };

  const passInfo = t_i18n(
    "On by default once OpenCRQ is connected. A scheduled pass reads the evidence health of the supporting assets of every risk scenario and records evidence past its re-test delay and assets with no evidence as CTEM signals.",
  );

  return (
    <SettingsCard
      id={anchorId}
      className={SECTION_BAR_ANCHOR_CLASS}
      data-anchor="card"
      tabIndex={-1}
      {...(draft ? { meta: fold?.meta } : fold)}
      data-testid="settings.organization.evidence-health"
      icon={CardIcon}
      title={t_i18n("Evidence health")}
      description={t_i18n("The OpenCRQ pass that finds decayed evidence and blind spots.")}
      action={
        !draft && isAdmin ? (
          <Button
            variant="secondary"
            className="gap-1.5"
            disabled={!profile}
            aria-label={t_i18n("Edit {name}", { name: t_i18n("Evidence health") })}
            onClick={start}
            data-testid="settings.organization.edit-evidence-health"
          >
            <Icon name="pencil" size={16} aria-hidden /> {t_i18n("Edit")}
          </Button>
        ) : undefined
      }
    >
      {!draft ? (
        <SettingsReadGrid>
          <SettingsReadField
            label={t_i18n("Evidence health pass")}
            info={passInfo}
            value={statusText}
            data-testid="settings.organization.evidence-health.status"
          />
          <SettingsReadField label={t_i18n("OpenCRQ integration")} value={declared ? nameOf(declared) : t_i18n("The single connected one")} />
          <SettingsReadField
            label={t_i18n("Re-test delay")}
            value={t_i18n("{count, plural, one {# day} other {# days}}", { count: pass.max_age_days })}
          />
          <SettingsReadField
            label={t_i18n("Pass cadence")}
            value={t_i18n("{count, plural, one {Every hour} other {Every # hours}}", { count: pass.cadence_hours })}
          />
        </SettingsReadGrid>
      ) : (
        <div className="space-y-4">
          <SettingsSwitchField
            id={`${id}-enabled`}
            label={t_i18n("Evidence health pass")}
            helper={t_i18n("Record decayed evidence and blind spots of the risk scenarios as signals.")}
            info={passInfo}
            checked={draft.enabled}
            onCheckedChange={(enabled) => setDraft({ ...draft, enabled })}
          />
          <SettingsFieldGrid>
            <SettingsField
              id={`${id}-integration`}
              label={t_i18n("OpenCRQ integration")}
              helper={
                candidates.length
                  ? t_i18n("The integration the pass reads, with its owner's credential.")
                  : t_i18n("No connected OpenCRQ integration. Connect one in Integrations first.")
              }
              error={pickMissing ? t_i18n("Several OpenCRQ integrations are connected: pick the one the pass reads.") : undefined}
            >
              <SearchableSelect
                id={`${id}-integration`}
                aria-labelledby={settingsFieldLabelId(`${id}-integration`)}
                aria-describedby={settingsFieldHintId(`${id}-integration`)}
                className="max-w-md"
                options={[
                  { value: AUTO, label: t_i18n("The single connected one") },
                  ...candidates.map((integration) => ({ value: integration.id, label: integration.name })),
                  ...(draft.integration !== AUTO && !candidates.some((integration) => integration.id === draft.integration)
                    ? [{ value: draft.integration, label: t_i18n("{name} (not connected)", { name: nameOf(draft.integration) }) }]
                    : []),
                ]}
                value={draft.integration}
                onValueChange={(integration) => setDraft({ ...draft, integration: integration || AUTO })}
              />
            </SettingsField>
            <SettingsField
              id={`${id}-max-age`}
              label={t_i18n("Re-test delay (days)")}
              helper={t_i18n("Evidence older than this has expired, 1 to 3650. Default 30.")}
              error={maxAge === null ? t_i18n("A whole number from {min} to {max}.", EVIDENCE_HEALTH_BOUNDS.max_age_days) : undefined}
            >
              <Input
                id={`${id}-max-age`}
                inputMode="numeric"
                aria-labelledby={settingsFieldLabelId(`${id}-max-age`)}
                aria-describedby={settingsFieldHintId(`${id}-max-age`)}
                aria-invalid={maxAge === null ? true : undefined}
                value={draft.maxAge}
                onChange={(e) => setDraft({ ...draft, maxAge: e.target.value })}
              />
            </SettingsField>
            <SettingsField
              id={`${id}-cadence`}
              label={t_i18n("Pass cadence (hours)")}
              helper={t_i18n("Hours between two passes, 1 to 720. Default 24.")}
              error={cadence === null ? t_i18n("A whole number from {min} to {max}.", EVIDENCE_HEALTH_BOUNDS.cadence_hours) : undefined}
            >
              <Input
                id={`${id}-cadence`}
                inputMode="numeric"
                aria-labelledby={settingsFieldLabelId(`${id}-cadence`)}
                aria-describedby={settingsFieldHintId(`${id}-cadence`)}
                aria-invalid={cadence === null ? true : undefined}
                value={draft.cadence}
                onChange={(e) => setDraft({ ...draft, cadence: e.target.value })}
              />
            </SettingsField>
          </SettingsFieldGrid>
          <SettingsSaveBar dirty={dirty} saving={save.isPending} saveDisabled={!dirty || blocked} onSave={onSave} onCancel={() => setDraft(null)} />
        </div>
      )}
    </SettingsCard>
  );
}
