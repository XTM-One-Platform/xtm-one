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
 * Create or change a unit of work (#9448): a custom unit's every field, or a
 * built-in unit's minutes alone (its customization; a customized one opens
 * on the fields it changed from the default, #9836 R3b). The checks are the server's
 * (`roi-catalog-form.ts`), said under each field; a server refusal is one
 * `role="alert"` line above the actions.
 */
import { useState } from "react";

import { RoiDefaultDiff } from "@/components/roi/RoiDefaultDiff";
import { originOf, useCustomizedLine } from "@/components/roi/roi-origin";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { focusFirstInvalidField, SettingsField, SettingsFieldGrid, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import {
  useCreateRoiUnit,
  useOverrideRoiBaseline,
  useUpdateRoiUnit,
  type RoiUnit,
} from "@/hooks/api/useRoiCatalogApi";
import { useFormatter } from "@/i18n";
import { defaultRateRole, FORM_MESSAGES, minutesErrors, parseNumber, unitKeyError, unitKeyFromName, type MinutesDraft, type MinutesField } from "./roi-catalog-form";
import { Spinner } from "@filigran/design-system";

interface RoiUnitDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** `null`: a new custom unit. */
  unit: RoiUnit | null;
  rateRoles: readonly string[];
}

const MINUTES_FIELDS: readonly MinutesField[] = ["low", "likely", "high", "review"];

/** @i18n-keys */
const MINUTES_LABELS = {
  low: "Manual minutes, low",
  likely: "Manual minutes, likely",
  high: "Manual minutes, high",
  review: "Review minutes",
} as const;

/** @i18n-keys */
const MINUTES_INFO = {
  low: "The shortest time a person takes to do one unit by hand.",
  likely: "The usual time a person takes to do one unit by hand. The ROI figures use it.",
  high: "The longest time a person takes to do one unit by hand.",
  review: "The time a person spends checking one unit an agent did. Subtracted from the time saved.",
} as const;

/** The visible help line under each minutes field, with an example (UX round 1 B4). @i18n-keys */
const MINUTES_HELP = {
  low: "The fastest case, in minutes. Usually 10.",
  likely: "The usual case, in minutes. Usually 20.",
  high: "The slowest case, in minutes. Usually 45.",
  review: "Checking one unit an agent did, in minutes. Usually 5.",
} as const;

function draftOf(unit: RoiUnit | null): MinutesDraft {
  return {
    low: unit ? String(unit.manual_min_low) : "",
    likely: unit ? String(unit.manual_min_likely) : "",
    high: unit ? String(unit.manual_min_high) : "",
    review: unit ? String(unit.review_min) : "",
  };
}

export function RoiUnitDialog({ open, onOpenChange, unit, rateRoles }: RoiUnitDialogProps) {
  const { t_i18n } = useFormatter();
  // The diff reads like the row: minutes with their unit (UX round 1 B3).
  const mins = (minutes: number) => t_i18n("{minutes} min", { minutes });
  const customizedLine = useCustomizedLine();
  const builtin = unit?.builtin ?? false;
  // Compare with default: a customized built-in shows default -> current of each changed field.
  const comparing = builtin && unit && unit.default && originOf(unit) === "customized" ? { unit, base: unit.default } : null;
  const [name, setName] = useState(unit?.name ?? "");
  const [key, setKey] = useState(unit?.key ?? "");
  const [keyTouched, setKeyTouched] = useState(Boolean(unit));
  const [role, setRole] = useState(unit?.role ?? "");
  // A new unit starts on a role the list offers: the CTEM catalog has `analyst`,
  // the corporate one (Copilot mode, #9550) starts at `knowledge_worker`.
  const [rateRole, setRateRole] = useState(unit?.rate_role ?? defaultRateRole(rateRoles));
  const [source, setSource] = useState(unit?.source ?? "");
  const [minutes, setMinutes] = useState<MinutesDraft>(draftOf(unit));
  const [submitted, setSubmitted] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const create = useCreateRoiUnit();
  const update = useUpdateRoiUnit();
  const override = useOverrideRoiBaseline();
  const saving = create.isPending || update.isPending || override.isPending;

  const minuteErrors = minutesErrors(minutes);
  const nameError = !builtin && !name.trim() ? FORM_MESSAGES.nameRequired : undefined;
  const keyError = !unit ? unitKeyError(key) : undefined;
  const invalid = Boolean(nameError || keyError || Object.keys(minuteErrors).length);

  const onNameChange = (value: string) => {
    setName(value);
    if (!keyTouched) setKey(unitKeyFromName(value));
  };

  const onSave = () => {
    setSubmitted(true);
    setServerError(null);
    if (invalid) {
      focusFirstInvalidField([
        nameError && "roi-unit-name",
        keyError && "roi-unit-key",
        ...MINUTES_FIELDS.map((field) => minuteErrors[field] && `roi-unit-${field}`),
      ]);
      return;
    }
    const figures = {
      manual_min_low: parseNumber(minutes.low)!,
      manual_min_likely: parseNumber(minutes.likely)!,
      manual_min_high: parseNumber(minutes.high)!,
      review_min: parseNumber(minutes.review)!,
      source: source.trim(),
    };
    const done = { onSuccess: () => onOpenChange(false), onError: (err: Error) => setServerError(err.message) };
    if (builtin && unit) override.mutate({ unit: unit.key, ...figures }, done);
    else if (unit) update.mutate({ key: unit.key, name: name.trim(), role: role.trim(), rate_role: rateRole, ...figures }, done);
    else create.mutate({ key, name: name.trim(), role: role.trim(), rate_role: rateRole, ...figures }, done);
  };

  const shown = (error: string | undefined) => (submitted && error ? t_i18n(error) : undefined);
  const title = builtin ? t_i18n("Customize {name}", { name: unit!.name }) : unit ? t_i18n("Edit {name}", { name: unit.name }) : t_i18n("New unit of work");

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            {builtin
              ? t_i18n("A built-in unit keeps its name and role. Set the minutes your organization measured; Reset to default returns to the catalog's.")
              : t_i18n("A unit of work is one thing an agent does that a person would otherwise do by hand, such as a contract reviewed or a ticket resolved.")}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="-m-1 space-y-4 p-1">
          {comparing && (
            <RoiDefaultDiff
              line={customizedLine(comparing.unit)}
              fields={[
                { label: "Manual minutes, low", default: mins(comparing.base.manual_min_low), current: mins(comparing.unit.manual_min_low) },
                { label: "Manual minutes, likely", default: mins(comparing.base.manual_min_likely), current: mins(comparing.unit.manual_min_likely) },
                { label: "Manual minutes, high", default: mins(comparing.base.manual_min_high), current: mins(comparing.unit.manual_min_high) },
                { label: "Review minutes", default: mins(comparing.base.review_min), current: mins(comparing.unit.review_min) },
              ]}
            />
          )}
          {!builtin && (
            <SettingsFieldGrid>
              <SettingsField id="roi-unit-name" label={t_i18n("Name")} required helper={t_i18n("What one unit is, such as Contract reviewed.")} error={shown(nameError)}>
                <Input id="roi-unit-name" value={name} onChange={(e) => onNameChange(e.target.value)} aria-labelledby={settingsFieldLabelId("roi-unit-name")} aria-describedby={settingsFieldHintId("roi-unit-name")} />
              </SettingsField>
              <SettingsField
                id="roi-unit-key"
                label={t_i18n("Key")}
                required
                helper={unit ? t_i18n("The key cannot change: declarations and records name it.") : t_i18n("What declarations and exports name. Cannot change later.")}
                error={shown(keyError)}
              >
                <Input
                  id="roi-unit-key"
                  value={key}
                  disabled={Boolean(unit)}
                  onChange={(e) => {
                    setKeyTouched(true);
                    setKey(e.target.value);
                  }}
                  aria-labelledby={settingsFieldLabelId("roi-unit-key")} aria-describedby={settingsFieldHintId("roi-unit-key")}
                />
              </SettingsField>
              <SettingsField id="roi-unit-role" label={t_i18n("Who does it by hand")} helper={t_i18n("A job title, such as Legal counsel.")}>
                <Input id="roi-unit-role" value={role} onChange={(e) => setRole(e.target.value)} aria-labelledby={settingsFieldLabelId("roi-unit-role")} aria-describedby={settingsFieldHintId("roi-unit-role")} />
              </SettingsField>
              <SettingsField id="roi-unit-rate-role" label={t_i18n("Rate role")} helper={t_i18n("The hourly rate the time saved is valued at, from Rates by role.")}>
                <SearchableSelect
                  id="roi-unit-rate-role"
                  aria-labelledby={settingsFieldLabelId("roi-unit-rate-role")}
                  aria-describedby={settingsFieldHintId("roi-unit-rate-role")}
                  searchable="auto"
                  options={rateRoles.map((r) => ({ value: r, label: r }))}
                  value={rateRole}
                  onValueChange={setRateRole}
                />
              </SettingsField>
            </SettingsFieldGrid>
          )}
          <SettingsFieldGrid>
            {MINUTES_FIELDS.map((field) => (
              <SettingsField
                key={field}
                id={`roi-unit-${field}`}
                label={t_i18n(MINUTES_LABELS[field])}
                info={t_i18n(MINUTES_INFO[field])}
                helper={t_i18n(MINUTES_HELP[field])}
                required
                error={shown(minuteErrors[field])}
              >
                <div className="relative">
                  <Input
                    id={`roi-unit-${field}`}
                    inputMode="decimal"
                    className="[&_input]:pr-12"
                    value={minutes[field]}
                    onChange={(e) => setMinutes((m) => ({ ...m, [field]: e.target.value }))}
                    aria-invalid={shown(minuteErrors[field]) ? true : undefined}
                    aria-labelledby={settingsFieldLabelId(`roi-unit-${field}`)} aria-describedby={settingsFieldHintId(`roi-unit-${field}`)}
                  />
                  <span aria-hidden className="pointer-events-none absolute inset-y-0 right-3 flex items-center content-compact text-default-secondary">
                    {t_i18n("min")}
                  </span>
                </div>
              </SettingsField>
            ))}
            <SettingsField id="roi-unit-source" label={t_i18n("Source of the minutes")} helper={t_i18n("Where the figures come from: a time study, a team estimate.")} wide>
              <Input id="roi-unit-source" value={source} onChange={(e) => setSource(e.target.value)} aria-labelledby={settingsFieldLabelId("roi-unit-source")} aria-describedby={settingsFieldHintId("roi-unit-source")} />
            </SettingsField>
          </SettingsFieldGrid>
        </DialogBody>
        <DialogFooter>
          {serverError && (
            <p role="alert" className="mr-auto content-compact text-feedback-error-primary">
              {serverError}
            </p>
          )}
          <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={saving}>
            {t_i18n("Cancel")}
          </Button>
          <Button onClick={onSave} disabled={saving} aria-busy={saving || undefined} className="gap-1.5">
            {saving && <Spinner size="sm" tone="inherit" />}
            {t_i18n("Save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
