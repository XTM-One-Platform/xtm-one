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
 * Settings > Organization > Rates by role: "Value visibility and privacy" (#12077 slice L6b,
 * design dev-docs/roi-outcomes.md 12.5 and decision D12-1). The three ROI settings
 * `PUT /roi/settings` stores: the organization totals for every user, the k of the anonymity
 * rule (a fleet, agent, unit or week backed by fewer people is folded into "Other" for
 * non-administrators) and the administrators' "By person" breakdown, audited on every read.
 * Read view at rest, Edit opens the fields, Save or Cancel returns (13.au). No run, no model.
 */
import { useId, useState } from "react";
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  SettingsCard,
  SettingsField,
  SettingsSaveBar,
  SettingsSwitchField,
  focusFirstInvalidField,
  settingsFieldHintId,
  settingsFieldLabelId,
} from "@/components/ui/settings-form";
import { SettingsReadField, SettingsReadGrid } from "@/components/ui/settings-read-view";
import { ROI_MIN_GROUP_SIZE_BOUNDS, useRoiSettings, useSaveRoiSettings } from "@/hooks/api/useRoiReportApi";
import { useFormatter } from "@/i18n";
import { parseMinGroupSize } from "@/lib/roi-settings";

function CardIcon({ className }: { className?: string }) {
  return (
    <span className={className}>
      <Icon name="shield-check" size={16} />
    </span>
  );
}

/** Settings > Organization is administrators only, like the PUT it calls. */
export function RoiPrivacyCard() {
  const { t_i18n } = useFormatter();
  const id = useId();
  const { data, isError } = useRoiSettings();
  const save = useSaveRoiSettings();
  const [editing, setEditing] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [orgDraft, setOrgDraft] = useState(false);
  const [kDraft, setKDraft] = useState("");
  const [byPersonDraft, setByPersonDraft] = useState(false);

  const orgVisible = data?.roi_org_totals_visible ?? false;
  const k = data?.roi_min_group_size ?? ROI_MIN_GROUP_SIZE_BOUNDS.fallback;
  const byPerson = data?.roi_by_person_visible ?? false;
  const kValue = parseMinGroupSize(kDraft);
  const kError =
    submitted && kValue === null
      ? t_i18n("A whole number from {min} to {max}.", { min: ROI_MIN_GROUP_SIZE_BOUNDS.min, max: ROI_MIN_GROUP_SIZE_BOUNDS.max })
      : undefined;

  const orgInfo = t_i18n(
    "On: every user may switch the Value page to the organization's totals, the same figure an administrator reads. Off: only administrators see them; every user still sees their own.",
  );
  const kInfo = t_i18n(
    "For users who are not administrators, a fleet, agent, unit of work or week backed by fewer people than this is folded into \"Other\", so nobody's work can be singled out. Administrators always see every row. Exports and agent tools apply the same rule.",
  );
  const byPersonInfo = t_i18n(
    "On: administrators see a By person breakdown of the organization's value. Every read of it is written to the audit log. Users who are not administrators never see another person's figures.",
  );

  const startEdit = () => {
    setOrgDraft(orgVisible);
    setKDraft(String(k));
    setByPersonDraft(byPerson);
    setSubmitted(false);
    setEditing(true);
  };
  const onSave = () => {
    setSubmitted(true);
    if (kValue === null) {
      focusFirstInvalidField([`${id}-k`]);
      return;
    }
    save.mutate(
      { roi_org_totals_visible: orgDraft, roi_min_group_size: kValue, roi_by_person_visible: byPersonDraft },
      { onSuccess: () => setEditing(false) },
    );
  };
  const dirty = orgDraft !== orgVisible || kDraft.trim() !== String(k) || byPersonDraft !== byPerson;

  return (
    <SettingsCard
      data-testid="settings.roi.privacy"
      icon={CardIcon}
      title={t_i18n("Value visibility and privacy")}
      description={t_i18n("Who sees the organization's value figures, and how small a group may be shown apart.")}
      action={
        !editing ? (
          <Button
            variant="secondary"
            className="gap-1.5"
            aria-label={t_i18n("Edit value visibility and privacy")}
            disabled={!data}
            onClick={startEdit}
          >
            <Icon name="pencil" size={16} aria-hidden /> {t_i18n("Edit")}
          </Button>
        ) : undefined
      }
    >
      {isError && !data ? (
        <p role="alert" className="content-compact text-feedback-error-primary" data-testid="settings.roi.privacy.error">
          {t_i18n("Could not read the value settings.")}
        </p>
      ) : !editing ? (
        <SettingsReadGrid>
          <SettingsReadField
            label={t_i18n("Organization totals for every user")}
            info={orgInfo}
            value={orgVisible ? t_i18n("On") : t_i18n("Off")}
          />
          <SettingsReadField
            label={t_i18n("Smallest group shown apart")}
            info={kInfo}
            value={t_i18n("{count, plural, one {# person} other {# people}}", { count: k })}
          />
          <SettingsReadField
            label={t_i18n("By person breakdown for administrators")}
            info={byPersonInfo}
            value={byPerson ? t_i18n("On") : t_i18n("Off")}
          />
        </SettingsReadGrid>
      ) : (
        <div className="space-y-3">
          <SettingsSwitchField
            id={`${id}-org`}
            label={t_i18n("Organization totals for every user")}
            helper={t_i18n("Off: only administrators see the organization's figures.")}
            info={orgInfo}
            checked={orgDraft}
            onCheckedChange={setOrgDraft}
          />
          <SettingsField
            id={`${id}-k`}
            label={t_i18n("Smallest group shown apart")}
            info={kInfo}
            helper={t_i18n("People, from {min} to {max}. Smaller groups are folded into \"Other\".", {
              min: ROI_MIN_GROUP_SIZE_BOUNDS.min,
              max: ROI_MIN_GROUP_SIZE_BOUNDS.max,
            })}
            error={kError}
          >
            <Input
              id={`${id}-k`}
              aria-labelledby={settingsFieldLabelId(`${id}-k`)}
              aria-describedby={settingsFieldHintId(`${id}-k`)}
              aria-invalid={kError ? true : undefined}
              type="number"
              inputMode="numeric"
              min={ROI_MIN_GROUP_SIZE_BOUNDS.min}
              max={ROI_MIN_GROUP_SIZE_BOUNDS.max}
              step={1}
              value={kDraft}
              onChange={(e) => setKDraft(e.target.value)}
              className="w-32"
            />
          </SettingsField>
          <SettingsSwitchField
            id={`${id}-by-person`}
            label={t_i18n("By person breakdown for administrators")}
            helper={t_i18n("Every read is written to the audit log.")}
            info={byPersonInfo}
            checked={byPersonDraft}
            onCheckedChange={setByPersonDraft}
          />
          {save.isError && (
            <p role="alert" className="content-compact text-feedback-error-primary">
              {t_i18n("Could not save the value settings.")}
            </p>
          )}
          <SettingsSaveBar dirty={dirty} saving={save.isPending} onSave={onSave} onCancel={() => setEditing(false)} />
        </div>
      )}
    </SettingsCard>
  );
}
