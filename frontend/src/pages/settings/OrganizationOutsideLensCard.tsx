/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * Settings > Organization > Exposure: "Owned domains and outside lens" (CTEM-26 slice 2, #1140).
 * The owned domains (also read by the CTEM-29 scope gate), the public address ranges
 * and the opt-in attack surface pass (`dev-docs/ctem-attack-surface.md` s.1). Read view
 * at rest; Edit (administrators, like the PUT) opens the fields; Save writes a new
 * profile version with these four fields alone changed.
 */

import { useId, useState } from "react";
import { Icon } from "@filigran/design-system";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  SettingsCard,
  SettingsField,
  SettingsFieldGrid,
  SettingsSaveBar,
  SettingsSwitchField,
  settingsFieldHintId,
} from "@/components/ui/settings-form";
import { SettingsReadField, SettingsReadGrid } from "@/components/ui/settings-read-view";
import { TagInput } from "@/components/ui/tag-input";
import { useToast } from "@/components/ui/toast";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { type OrganizationProfile, useAttackSurfaceProviders, useOrganizationContext, useSaveOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import {
  ATTACK_SURFACE_BOUNDS,
  type AttackSurfacePass,
  DEFAULT_ATTACK_SURFACE,
  fromForm,
  invalidAddressRanges,
  normalizeAddressRange,
  normalizeDomain,
  toForm,
} from "@/lib/organization-profile";

const NO_PROVIDER = "__none__";

function CardIcon({ className }: { className?: string }) {
  return (
    <span className={className}>
      <Icon name="network" size={16} />
    </span>
  );
}

interface Draft {
  owned_domains: string[];
  address_ranges: string[];
  attack_surface: AttackSurfacePass;
  cadence: string;
  stale: string;
}

/** A whole number in the bounds, or null. */
export function boundedInteger(text: string, bounds: { min: number; max: number }): number | null {
  if (!/^\d+$/.test(text.trim())) return null;
  const n = Number(text.trim());
  return n >= bounds.min && n <= bounds.max ? n : null;
}

/** The domains as the backend stores them, each once. */
export function cleanDomains(values: readonly string[]): string[] {
  return [...new Set(values.map(normalizeDomain).filter((d): d is string => d !== null))];
}

/** The address ranges in their network form, each once; the refused ones are kept out (Save is blocked on them). */
export function cleanRanges(values: readonly string[]): string[] {
  return [...new Set(values.map(normalizeAddressRange).filter((r): r is string => r !== null))];
}

export function OrganizationOutsideLensCard() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const id = useId();
  const { isAdmin } = useAuth();
  const { data } = useOrganizationContext();
  const save = useSaveOrganizationContext();
  const { data: supported } = useAttackSurfaceProviders();
  const { data: integrations = [] } = useIntegrations();
  const [draft, setDraft] = useState<Draft | null>(null);

  const profile = data?.profile;
  const domains = profile?.owned_domains ?? [];
  const ranges = profile?.address_ranges ?? [];
  const pass = { ...DEFAULT_ATTACK_SURFACE, ...(profile?.attack_surface ?? {}) };
  const providers = new Set(supported?.providers ?? []);
  // Only a connected integration of a provider the pass reads is offered.
  const candidates = integrations.filter((integration) => providers.has(integration.provider) && integration.status === "connected");
  const chosen = integrations.find((integration) => integration.id === pass.integration);
  const providerText = chosen ? chosen.name : pass.integration ? t_i18n("Integration not found") : t_i18n("None");

  const start = () =>
    setDraft({
      owned_domains: [...domains],
      address_ranges: [...ranges],
      attack_surface: { ...pass },
      cadence: String(pass.cadence_hours),
      stale: String(pass.stale_after_days),
    });

  const badRanges = draft ? invalidAddressRanges(draft.address_ranges) : [];
  const cadence = draft ? boundedInteger(draft.cadence, ATTACK_SURFACE_BOUNDS.cadence_hours) : null;
  const stale = draft ? boundedInteger(draft.stale, ATTACK_SURFACE_BOUNDS.stale_after_days) : null;
  const providerMissing = Boolean(draft?.attack_surface.enabled && !candidates.some((i) => i.id === draft.attack_surface.integration));
  const next =
    draft && cadence !== null && stale !== null
      ? {
          owned_domains: cleanDomains(draft.owned_domains),
          address_ranges: cleanRanges(draft.address_ranges),
          attack_surface: { ...draft.attack_surface, cadence_hours: cadence, stale_after_days: stale },
        }
      : null;
  const dirty =
    draft !== null &&
    (next === null ||
      JSON.stringify(next) !== JSON.stringify({ owned_domains: domains, address_ranges: ranges, attack_surface: pass }));
  const blocked = next === null || badRanges.length > 0 || providerMissing;

  const onSave = () => {
    if (!profile || !next || blocked) return;
    const parsed = fromForm({ ...toForm(profile), ...next });
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

  const domainsInfo = t_i18n(
    "The DNS and email domains the organization owns. The CTEM scope gate reads them (a leaked credential of an account on one of them is in scope) and the outside lens looks them up.",
  );
  const rangesInfo = t_i18n(
    "Public IP addresses or CIDR blocks the organization owns. A name of an owned domain that points outside them is reported as no longer owned. A bare address is saved as its /32 or /128.",
  );
  const lensInfo = t_i18n(
    "Off by default. On: a scheduled pass reads what the provider sees of the owned domains and address ranges and records new exposures, stale or orphaned assets and surface drift as CTEM signals.",
  );

  return (
    <SettingsCard
      data-testid="settings.organization.outside-lens"
      icon={CardIcon}
      title={t_i18n("Owned domains and outside lens")}
      description={t_i18n("What the organization owns on the Internet, and the attack surface pass that watches it.")}
      action={
        !draft && isAdmin ? (
          <Button
            variant="secondary"
            className="gap-1.5"
            disabled={!profile}
            aria-label={t_i18n("Edit {name}", { name: t_i18n("Owned domains and outside lens") })}
            onClick={start}
            data-testid="settings.organization.edit-outside-lens"
          >
            <Icon name="pencil" size={16} aria-hidden /> {t_i18n("Edit")}
          </Button>
        ) : undefined
      }
    >
      {!draft ? (
        <SettingsReadGrid>
          <SettingsReadField
            label={t_i18n("Owned domains")}
            info={domainsInfo}
            value={domains.length ? domains.join(", ") : t_i18n("None")}
            wide
            data-testid="settings.organization.outside-lens.domains"
          />
          <SettingsReadField
            label={t_i18n("Owned address ranges")}
            info={rangesInfo}
            value={ranges.length ? ranges.join(", ") : t_i18n("None")}
            mono={ranges.length > 0}
            wide
            data-testid="settings.organization.outside-lens.ranges"
          />
          <SettingsReadField
            label={t_i18n("Outside lens")}
            info={lensInfo}
            value={
              pass.enabled ? <Badge variant="success">{t_i18n("On")}</Badge> : <Badge variant="secondary">{t_i18n("Off")}</Badge>
            }
            data-testid="settings.organization.outside-lens.enabled"
          />
          <SettingsReadField label={t_i18n("Attack surface provider")} value={providerText} />
          <SettingsReadField
            label={t_i18n("Pass cadence")}
            value={t_i18n("{count, plural, one {Every hour} other {Every # hours}}", { count: pass.cadence_hours })}
          />
          <SettingsReadField
            label={t_i18n("Stale after")}
            value={t_i18n("{count, plural, one {# day unseen} other {# days unseen}}", { count: pass.stale_after_days })}
          />
        </SettingsReadGrid>
      ) : (
        <div className="space-y-4">
          <SettingsFieldGrid>
            <SettingsField
              id={`${id}-domains`}
              label={t_i18n("Owned domains")}
              info={domainsInfo}
              helper={t_i18n("Also the scope of leaked credentials (CTEM scope gate). Type a domain and press Enter.")}
              wide
            >
              <TagInput
                id={`${id}-domains`}
                aria-describedby={settingsFieldHintId(`${id}-domains`)}
                value={draft.owned_domains}
                onChange={(owned_domains) => setDraft({ ...draft, owned_domains })}
                placeholder={t_i18n("example.com")}
                suggestFromPlatformTags={false}
                emptyLabel={t_i18n("None")}
              />
            </SettingsField>
            <SettingsField
              id={`${id}-ranges`}
              label={t_i18n("Owned address ranges")}
              info={rangesInfo}
              helper={t_i18n("An IP address or a CIDR block, such as 198.51.100.0/24 or 2001:db8::/32. Press Enter after each.")}
              error={
                badRanges.length
                  ? t_i18n("address range {value} is not an IP address or CIDR block", { value: badRanges[0] })
                  : undefined
              }
              wide
            >
              <TagInput
                id={`${id}-ranges`}
                aria-describedby={settingsFieldHintId(`${id}-ranges`)}
                aria-invalid={badRanges.length ? true : undefined}
                invalidTags={badRanges}
                value={draft.address_ranges}
                onChange={(address_ranges) => setDraft({ ...draft, address_ranges })}
                placeholder={t_i18n("198.51.100.0/24")}
                preserveCase
                suggestFromPlatformTags={false}
                emptyLabel={t_i18n("None")}
              />
            </SettingsField>
          </SettingsFieldGrid>
          <SettingsSwitchField
            className="rounded-sm bg-elevation-default p-3.5"
            id={`${id}-enabled`}
            label={t_i18n("Outside lens")}
            helper={t_i18n("Watch the owned domains and ranges from the Internet with an attack surface provider.")}
            info={lensInfo}
            checked={draft.attack_surface.enabled}
            onCheckedChange={(enabled) => setDraft({ ...draft, attack_surface: { ...draft.attack_surface, enabled } })}
          />
          <SettingsFieldGrid>
            <SettingsField
              id={`${id}-provider`}
              label={t_i18n("Attack surface provider")}
              helper={
                candidates.length
                  ? t_i18n("A connected integration the pass can read.")
                  : t_i18n("No connected attack surface integration. Connect one in Integrations first.")
              }
              error={providerMissing ? t_i18n("Pick a provider to turn the outside lens on.") : undefined}
            >
              <Select
                value={draft.attack_surface.integration || NO_PROVIDER}
                onValueChange={(value) =>
                  setDraft({ ...draft, attack_surface: { ...draft.attack_surface, integration: value === NO_PROVIDER ? "" : value } })
                }
              >
                <SelectTrigger id={`${id}-provider`} aria-describedby={settingsFieldHintId(`${id}-provider`)} aria-invalid={providerMissing ? true : undefined}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NO_PROVIDER}>{t_i18n("None")}</SelectItem>
                  {candidates.map((integration) => (
                    <SelectItem key={integration.id} value={integration.id}>
                      {integration.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </SettingsField>
            <SettingsField
              id={`${id}-cadence`}
              label={t_i18n("Pass cadence (hours)")}
              helper={t_i18n("Hours between two passes, 1 to 720. Default 24.")}
              error={cadence === null ? t_i18n("A whole number from {min} to {max}.", ATTACK_SURFACE_BOUNDS.cadence_hours) : undefined}
            >
              <Input
                id={`${id}-cadence`}
                inputMode="numeric"
                aria-describedby={settingsFieldHintId(`${id}-cadence`)}
                aria-invalid={cadence === null ? true : undefined}
                value={draft.cadence}
                onChange={(e) => setDraft({ ...draft, cadence: e.target.value })}
              />
            </SettingsField>
            <SettingsField
              id={`${id}-stale`}
              label={t_i18n("Stale after (days)")}
              helper={t_i18n("A host unseen this long is a stale asset, 1 to 365. Default 14.")}
              error={stale === null ? t_i18n("A whole number from {min} to {max}.", ATTACK_SURFACE_BOUNDS.stale_after_days) : undefined}
            >
              <Input
                id={`${id}-stale`}
                inputMode="numeric"
                aria-describedby={settingsFieldHintId(`${id}-stale`)}
                aria-invalid={stale === null ? true : undefined}
                value={draft.stale}
                onChange={(e) => setDraft({ ...draft, stale: e.target.value })}
              />
            </SettingsField>
          </SettingsFieldGrid>
          <SettingsSaveBar dirty={dirty} saving={save.isPending} saveDisabled={!dirty || blocked} onSave={onSave} onCancel={() => setDraft(null)} />
        </div>
      )}
    </SettingsCard>
  );
}
