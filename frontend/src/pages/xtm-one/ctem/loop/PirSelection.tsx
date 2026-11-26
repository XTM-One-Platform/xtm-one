// SPDX-License-Identifier: Apache-2.0

/**
 * The PIRs one path's PIR playbook listens to (#2436, dev-docs/ctem-signals.md section 2bis): the
 * enabled PIRs of the Intelligence policy, the path's default (the PIRs of its OpenCTI type, else every
 * enabled PIR) until an administrator picks them, each at its own minimum score. OpenCTI offering no PIR
 * of the path's type says "None available"; an empty pick provisions no playbook for the path.
 */
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { MultiSelect } from "@/components/ui/multi-select";
import type { CtemSignalPath } from "@/hooks/api/useCtemSignalsApi";
import { type OrganizationProfile, useIntelligencePolicy } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { profileFieldHref } from "@/lib/organization-profile-tabs";
import { sortByLabel } from "@/lib/select-options";
import { pirSelection, withPirSelection } from "./signal-sources";

export function PirSelection({
  path,
  profile,
  isAdmin,
  saving,
  onSave,
}: {
  path: CtemSignalPath;
  profile: OrganizationProfile | undefined;
  isAdmin: boolean;
  saving: boolean;
  onSave: (next: Partial<OrganizationProfile>, done: string) => void;
}) {
  const { t_i18n, locale } = useFormatter();
  const navigate = useNavigate();
  const { data } = useIntelligencePolicy();
  const enabled = (data?.pirs ?? []).filter((p) => p.enabled && !p.gone_at);
  const { pirIds, available, isDefault } = pirSelection(path.id, enabled, profile?.signal_sources?.pirs);
  const byId = new Map(enabled.map((p) => [p.pir_id, p]));
  // One save per change of the field, with the whole next pick: Clear saves the empty pick once
  // (UX r2 B4 of #2454), never one save per removed PIR from a stale selection.
  const save = (next: string[] | null) =>
    onSave({ signal_sources: withPirSelection(profile?.signal_sources, path.id, next) }, t_i18n("PIR selection saved"));

  return (
    <div className="flex flex-col gap-1.5 content-compact" data-testid="ctem-path-source-pirs" data-default={isDefault}>
      <span className="font-medium">{t_i18n("PIRs this playbook listens to")}</span>
      {enabled.length === 0 ? (
        <span className="text-default-secondary" data-testid="ctem-path-source-pirs-none">
          {t_i18n("None available: no PIR is enabled on the Intelligence policy.")}
        </span>
      ) : (
        <>
          {isDefault && available.length === 0 && (
            <span className="text-default-secondary" data-testid="ctem-path-source-pirs-none-of-type">
              {t_i18n("None available of this path's type in OpenCTI: every enabled PIR is listened to by default.")}
            </span>
          )}
          <MultiSelect
            options={enabled.map((p) => ({ value: p.pir_id, label: p.name }))}
            selected={pirIds}
            onChange={save}
            placeholder={t_i18n("No PIR: no playbook on this path")}
            aria-label={t_i18n("PIRs this playbook listens to")}
            searchPlaceholder={t_i18n("Search PIRs")}
            emptyOptionsLabel={t_i18n("No PIR")}
            noMatchLabel={t_i18n("No match")}
            disabled={!isAdmin || saving}
          />
          <span className="text-default-secondary" data-testid="ctem-path-source-pirs-thresholds">
            {pirIds.length === 0
              ? t_i18n("No playbook is provisioned for this path.")
              : t_i18n("Each PIR records at its own minimum score: {list}", {
                  // In the chips' order, by name, never the id order (UX advisory A1 of #2436, #2506).
                  list: sortByLabel(pirIds, (id) => byId.get(id)?.name ?? id, locale)
                    .map((id) => `${byId.get(id)?.name ?? id} (${byId.get(id)?.min_score ?? 0})`)
                    .join(", "),
                })}
          </span>
          {isAdmin && !isDefault && (
            <span>
              <Button variant="ghost" size="sm" onClick={() => save(null)} disabled={saving} data-testid="ctem-path-source-pirs-reset">
                {t_i18n("Use the default")}
              </Button>
            </span>
          )}
        </>
      )}
      {isAdmin && (
        <span>
          <Button variant="ghost" size="sm" onClick={() => navigate(profileFieldHref("pir_ids"))} data-testid="ctem-path-source-pirs-policy">
            {t_i18n("Open the Intelligence policy")}
          </Button>
        </span>
      )}
    </div>
  );
}
