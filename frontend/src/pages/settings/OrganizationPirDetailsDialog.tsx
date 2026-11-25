// SPDX-License-Identifier: Apache-2.0

/**
 * The details of one PIR, opened from its row on the Prioritization policy card (#1974): the
 * intelligence question, its owner, how often it is reviewed and the primary assets it serves.
 * The card is the same on CTEM > Inventory > Exposure and on Settings, so the dialog is too.
 *
 * OpenCTI's PIR holds its name, description, type, rescan period, criteria and filters, none of
 * these four fields: they are XTM One's, stored in the profile's `pir_details` by PIR id (#1773).
 * Save sends `pir_details` alone through PATCH (#1738), the other PIRs' details kept as stored.
 * An administrator edits; anyone else reads.
 */

import { useState } from "react";
import { Spinner } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { MultiSelect } from "@/components/ui/multi-select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { SettingsReadField, SettingsReadGrid } from "@/components/ui/settings-read-view";
import { useToast } from "@/components/ui/toast";
import { useOrganizationContext, usePatchOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
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
const FIELD = "org-pir-detail";

interface Props {
  /** The PIR whose details are open; `null` closes the dialog. */
  pirId: string | null;
  /** The PIR's name as its row shows it, never its id. */
  pirName: string;
  /** An administrator edits; anyone else reads. */
  canEdit: boolean;
  onOpenChange: (open: boolean) => void;
}

export function OrganizationPirDetailsDialog({ pirId, pirName, canEdit, onOpenChange }: Props) {
  const { t_i18n } = useFormatter();
  const open = pirId !== null;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md" data-testid="settings.organization.pir-details-dialog">
        <DialogHeader>
          <DialogTitle>{t_i18n("PIR details: {name}", { name: pirName })}</DialogTitle>
          <DialogDescription>
            {t_i18n("The question this PIR answers, who owns it, how often it is reviewed and the primary assets it serves. OpenCTI keeps the PIR itself.")}
          </DialogDescription>
        </DialogHeader>
        {/* Keyed by the PIR, so a second PIR opens on its own stored values, never the previous draft. */}
        {pirId !== null && <PirDetailsBody key={pirId} pirId={pirId} canEdit={canEdit} onClose={() => onOpenChange(false)} />}
      </DialogContent>
    </Dialog>
  );
}

function PirDetailsBody({ pirId, canEdit, onClose }: { pirId: string; canEdit: boolean; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data } = useOrganizationContext();
  const save = usePatchOrganizationContext();
  const profile = data?.profile;
  const stored = profile?.pir_details ?? {};
  const [detail, setDetail] = useState<PirDetail>(() => stored[pirId] ?? emptyPirDetail());
  const primaryAssets = (profile?.primary_assets ?? []).map((pa) => pa.name).filter(Boolean);
  const problem = pirDetailProblem(detail);
  const pirIds = [...new Set(profile?.pir_ids ?? [])];
  const next = pirDetailsOf(pirIds, { ...stored, [pirId]: detail });
  const dirty = JSON.stringify(next) !== JSON.stringify(pirDetailsOf(pirIds, stored));
  const set = (patch: Partial<PirDetail>) => setDetail((prev) => ({ ...prev, ...patch }));
  const cadenceText = (cadence: ReviewCadence) => (cadence ? t_i18n(REVIEW_CADENCE_LABELS[cadence]) : "");

  const onSave = () => {
    if (problem || !dirty) return;
    // #1738: pir_details alone travels, never the asset rows a sync keeps.
    save.mutate(
      { fields: { pir_details: next }, based_on_version: data?.version ?? null },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Organization profile saved"), variant: "success" });
          onClose();
        },
      },
    );
  };

  if (!canEdit) {
    return (
      <>
        <DialogBody className="space-y-3">
          <SettingsReadGrid data-testid="settings.organization.pir-details-read">
            <SettingsReadField label={t_i18n("Question")} value={detail.question} wide />
            <SettingsReadField label={t_i18n("Owner")} value={detail.owner} />
            <SettingsReadField label={t_i18n("Review cadence")} value={cadenceText(detail.review_cadence)} />
            <SettingsReadField label={t_i18n("Primary assets served")} value={detail.primary_assets.join(", ")} wide />
          </SettingsReadGrid>
          <OrganizationProvenanceLine sources={data?.sources} field="pir_details" />
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            {t_i18n("Close")}
          </Button>
        </DialogFooter>
      </>
    );
  }

  return (
    <>
      <DialogBody className="-m-1 space-y-4 p-1">
        <SettingsField
          id={`${FIELD}-question`}
          label={t_i18n("Question")}
          helper={t_i18n("The intelligence question this PIR answers.")}
          info={t_i18n("Example: Which ransomware groups target European banks this quarter? Agents read it with the PIR in the organization context. Left empty, they read the PIR by its name alone.")}
          error={problem ? t_i18n(problem) : undefined}
        >
          <Input
            id={`${FIELD}-question`}
            aria-labelledby={settingsFieldLabelId(`${FIELD}-question`)}
            aria-describedby={settingsFieldHintId(`${FIELD}-question`)}
            aria-invalid={problem ? true : undefined}
            maxLength={REFERENCE_BOUNDS.question}
            value={detail.question}
            onChange={(e) => set({ question: e.target.value })}
          />
        </SettingsField>
        <SettingsField
          id={`${FIELD}-owner`}
          label={t_i18n("Owner")}
          helper={t_i18n("The team or person who answers for this PIR.")}
          info={t_i18n("Example: CTI team. Agents read it with the PIR in the organization context. Left empty, the PIR names no owner.")}
        >
          <Input
            id={`${FIELD}-owner`}
            aria-labelledby={settingsFieldLabelId(`${FIELD}-owner`)}
            aria-describedby={settingsFieldHintId(`${FIELD}-owner`)}
            maxLength={REFERENCE_BOUNDS.owner}
            value={detail.owner}
            onChange={(e) => set({ owner: e.target.value })}
          />
        </SettingsField>
        <SettingsField
          id={`${FIELD}-cadence`}
          label={t_i18n("Review cadence")}
          helper={t_i18n("How often the question is reviewed.")}
          info={t_i18n("Example: Quarterly. Agents read it with the PIR in the organization context. Left empty, the PIR has no review cadence.")}
        >
          <SearchableSelect
            id={`${FIELD}-cadence`}
            aria-labelledby={settingsFieldLabelId(`${FIELD}-cadence`)}
            aria-describedby={settingsFieldHintId(`${FIELD}-cadence`)}
            pinned={[{ value: NO_CADENCE, label: t_i18n("Not set") }]}
            options={REVIEW_CADENCES.filter(Boolean).map((cadence) => ({ value: cadence, label: t_i18n(REVIEW_CADENCE_LABELS[cadence]) }))}
            sort={false}
            value={detail.review_cadence || NO_CADENCE}
            onValueChange={(value) => set({ review_cadence: (value === NO_CADENCE ? "" : value) as ReviewCadence })}
          />
        </SettingsField>
        <SettingsField
          id={`${FIELD}-primary-assets`}
          label={t_i18n("Primary assets served")}
          helper={t_i18n("The primary assets of the inventory this PIR protects.")}
          info={t_i18n("Example: Payment platform, picked from the primary assets of the inventory. Agents read them with the PIR in the organization context. Left empty, the PIR names no primary asset.")}
        >
          <MultiSelect
            id={`${FIELD}-primary-assets`}
            aria-label={t_i18n("Primary assets served")}
            aria-describedby={settingsFieldHintId(`${FIELD}-primary-assets`)}
            options={[...new Set([...primaryAssets, ...detail.primary_assets])].map((name) => ({ value: name, label: name }))}
            selected={detail.primary_assets}
            onToggle={(name) =>
              set({ primary_assets: detail.primary_assets.includes(name) ? detail.primary_assets.filter((n) => n !== name) : [...detail.primary_assets, name] })
            }
            placeholder={t_i18n("None")}
            searchPlaceholder={t_i18n("Search a primary asset")}
            emptyOptionsLabel={t_i18n("No primary asset declared")}
            noMatchLabel={t_i18n("No match")}
          />
        </SettingsField>
        <OrganizationProvenanceLine sources={data?.sources} field="pir_details" />
        {save.isError && (
          <p role="alert" className="content-compact text-feedback-error-primary" data-testid="settings.organization.pir-details-error">
            {t_i18n("The organization profile was not saved: {reason}", { reason: save.error.message })}
          </p>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="secondary" onClick={onClose}>
          {t_i18n("Cancel")}
        </Button>
        <Button onClick={onSave} disabled={!dirty || Boolean(problem) || save.isPending || !profile} aria-busy={save.isPending || undefined}>
          {save.isPending && <Spinner size="sm" tone="inherit" />}
          {t_i18n("Save")}
        </Button>
      </DialogFooter>
    </>
  );
}
