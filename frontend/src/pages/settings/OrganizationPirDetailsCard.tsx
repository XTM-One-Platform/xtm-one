// SPDX-License-Identifier: Apache-2.0

/**
 * Settings > Organization > Identity and risk: "PIR details" (#1773).
 * What the organization says about each PIR it follows (OpenCTI keeps the PIR itself):
 * the intelligence question, its owner, how often it is reviewed and the crown jewels it
 * serves. One row per PIR of `pir_ids`, the PIR by its last known name. Edit
 * (administrators) opens the rows; Save sends `pir_details` alone through PATCH (#1738).
 */

import { useId, useState } from "react";
import { Target } from "lucide-react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { MultiSelect } from "@/components/ui/multi-select";
import { SECTION_BAR_ANCHOR_CLASS } from "@/components/ui/section-index";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsCard, SettingsField, SettingsFieldGrid, SettingsSaveBar, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { SettingsReadField, SettingsReadGrid } from "@/components/ui/settings-read-view";
import { useToast } from "@/components/ui/toast";
import { useOrganizationContext, usePatchOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import {
  emptyPirDetail,
  type PirDetail,
  pirDetailProblem,
  pirDetailsOf,
  REFERENCE_BOUNDS,
  REVIEW_CADENCE_LABELS,
  REVIEW_CADENCES,
  type ReviewCadence,
} from "@/lib/organization-reference";
import { OrganizationProvenanceLine } from "./OrganizationProvenanceLine";

const NO_CADENCE = "__none__";

export function OrganizationPirDetailsCard({ id: anchorId }: { id: string }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const id = useId();
  const { isAdmin } = useAuth();
  const { data } = useOrganizationContext();
  const save = usePatchOrganizationContext();
  const [draft, setDraft] = useState<Record<string, PirDetail> | null>(null);

  const profile = data?.profile;
  const pirIds = profile?.pir_ids ?? [];
  const names = profile?.pir_names ?? {};
  const stored = profile?.pir_details ?? {};
  const jewels = (profile?.crown_jewels ?? []).map((jewel) => jewel.name).filter(Boolean);
  const nameOf = (pirId: string) => names[pirId] || pirId;
  const detailOf = (pirId: string) => draft?.[pirId] ?? stored[pirId] ?? emptyPirDetail();

  const problems = draft ? pirIds.map((pirId) => pirDetailProblem(detailOf(pirId))) : [];
  const next = draft ? pirDetailsOf(pirIds, draft) : null;
  const dirty = next !== null && JSON.stringify(next) !== JSON.stringify(pirDetailsOf(pirIds, stored));
  const blocked = problems.some(Boolean);

  const setDetail = (pirId: string, patch: Partial<PirDetail>) =>
    draft && setDraft({ ...draft, [pirId]: { ...detailOf(pirId), ...patch } });

  const onSave = () => {
    if (!next || blocked) return;
    // #1738: pir_details alone travels, never the asset rows a sync keeps.
    save.mutate(
      { fields: { pir_details: next }, based_on_version: data?.version ?? null },
      {
        onSuccess: () => {
          setDraft(null);
          toast({ title: t_i18n("Organization profile saved"), variant: "success" });
        },
        onError: (err) => toast({ title: t_i18n("The organization profile was not saved"), description: err.message, variant: "error" }),
      },
    );
  };

  const cadenceText = (cadence: ReviewCadence) => (cadence ? t_i18n(REVIEW_CADENCE_LABELS[cadence]) : "");

  return (
    <SettingsCard
      id={anchorId}
      className={SECTION_BAR_ANCHOR_CLASS}
      data-anchor="card"
      tabIndex={-1}
      data-testid="settings.organization.pir-details"
      icon={Target}
      title={t_i18n("PIR details")}
      description={t_i18n("For each PIR followed: the question it answers, who owns it, how often it is reviewed and the crown jewels it serves. OpenCTI keeps the PIR itself.")}
      action={
        !draft && isAdmin && pirIds.length > 0 ? (
          <Button
            variant="secondary"
            className="gap-1.5"
            disabled={!profile}
            aria-label={t_i18n("Edit {name}", { name: t_i18n("PIR details") })}
            onClick={() => setDraft({ ...stored })}
            data-testid="settings.organization.edit-pir-details"
          >
            <Icon name="pencil" size={16} aria-hidden /> {t_i18n("Edit")}
          </Button>
        ) : undefined
      }
    >
      {pirIds.length === 0 ? (
        <p className="text-default-secondary">{t_i18n("No PIR followed. Pick the PIRs in Ticketing and intelligence first.")}</p>
      ) : (
        <div className="space-y-4">
          {pirIds.map((pirId, index) => {
            const detail = detailOf(pirId);
            const rowId = `${id}-pir-${index}`;
            if (!draft) {
              return (
                <div key={pirId} className="space-y-1" data-testid="settings.organization.pir-details.row">
                  <p className="font-medium break-words">{nameOf(pirId)}</p>
                  <SettingsReadGrid>
                    <SettingsReadField label={t_i18n("Question")} value={detail.question} wide />
                    <SettingsReadField label={t_i18n("Owner")} value={detail.owner} />
                    <SettingsReadField label={t_i18n("Review cadence")} value={cadenceText(detail.review_cadence)} />
                    <SettingsReadField label={t_i18n("Crown jewels served")} value={detail.crown_jewels.join(", ")} wide />
                  </SettingsReadGrid>
                </div>
              );
            }
            const problem = problems[index];
            return (
              <div key={pirId} className="space-y-2 border-t border-elevation-subtle-soft pt-3 first:border-t-0 first:pt-0" data-testid="settings.organization.pir-details.row">
                <p className="font-medium break-words">{nameOf(pirId)}</p>
                <SettingsFieldGrid>
                  <SettingsField
                    id={`${rowId}-question`}
                    label={t_i18n("Question")}
                    helper={t_i18n("The intelligence question this PIR answers.")}
                    error={problem ? t_i18n(problem) : undefined}
                    wide
                  >
                    <Input
                      id={`${rowId}-question`}
                      aria-labelledby={settingsFieldLabelId(`${rowId}-question`)}
                      aria-describedby={settingsFieldHintId(`${rowId}-question`)}
                      aria-invalid={problem ? true : undefined}
                      maxLength={REFERENCE_BOUNDS.question}
                      value={detail.question}
                      onChange={(e) => setDetail(pirId, { question: e.target.value })}
                    />
                  </SettingsField>
                  <SettingsField id={`${rowId}-owner`} label={t_i18n("Owner")}>
                    <Input
                      id={`${rowId}-owner`}
                      aria-labelledby={settingsFieldLabelId(`${rowId}-owner`)}
                      maxLength={REFERENCE_BOUNDS.owner}
                      value={detail.owner}
                      onChange={(e) => setDetail(pirId, { owner: e.target.value })}
                    />
                  </SettingsField>
                  <SettingsField id={`${rowId}-cadence`} label={t_i18n("Review cadence")}>
                    <SearchableSelect
                      id={`${rowId}-cadence`}
                      aria-labelledby={settingsFieldLabelId(`${rowId}-cadence`)}
                      pinned={[{ value: NO_CADENCE, label: t_i18n("Not set") }]}
                      options={REVIEW_CADENCES.filter(Boolean).map((cadence) => ({ value: cadence, label: t_i18n(REVIEW_CADENCE_LABELS[cadence]) }))}
                      sort={false}
                      value={detail.review_cadence || NO_CADENCE}
                      onValueChange={(value) => setDetail(pirId, { review_cadence: (value === NO_CADENCE ? "" : value) as ReviewCadence })}
                    />
                  </SettingsField>
                  <SettingsField id={`${rowId}-jewels`} label={t_i18n("Crown jewels served")} wide>
                    <MultiSelect
                      id={`${rowId}-jewels`}
                      aria-label={t_i18n("Crown jewels served")}
                      options={[...new Set([...jewels, ...detail.crown_jewels])].map((name) => ({ value: name, label: name }))}
                      selected={detail.crown_jewels}
                      onToggle={(name) =>
                        setDetail(pirId, {
                          crown_jewels: detail.crown_jewels.includes(name) ? detail.crown_jewels.filter((n) => n !== name) : [...detail.crown_jewels, name],
                        })
                      }
                      placeholder={t_i18n("None")}
                      searchPlaceholder={t_i18n("Search a crown jewel")}
                      emptyOptionsLabel={t_i18n("No crown jewel declared")}
                      noMatchLabel={t_i18n("No match")}
                    />
                  </SettingsField>
                </SettingsFieldGrid>
              </div>
            );
          })}
          {draft && <SettingsSaveBar dirty={dirty} saving={save.isPending} saveDisabled={!dirty || blocked} onSave={onSave} onCancel={() => setDraft(null)} />}
        </div>
      )}
      <OrganizationProvenanceLine sources={data?.sources} field="pir_details" />
    </SettingsCard>
  );
}
