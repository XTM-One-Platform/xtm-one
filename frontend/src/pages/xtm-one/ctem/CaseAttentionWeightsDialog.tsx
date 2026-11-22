// SPDX-License-Identifier: Apache-2.0

/**
 * "Ranking points" of the Cases attention score (#1176, slice 2b): an
 * administrator sets how many points each reason adds, saved in the
 * organization profile (`case_attention`) and read by the list, its "Act on
 * this first" band and the case page. "Reset to defaults" fills the shipped
 * points; nothing is saved until Save.
 */
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { useToast } from "@/components/ui/toast";
import { useOrganizationContext, useSaveOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { DEFAULT_CASE_ATTENTION_WEIGHTS, weightsFromProfile, weightsToProfile } from "./case-attention";
import type { CaseAttentionProfile } from "@/lib/organization-profile";

type Key = keyof CaseAttentionProfile;

/** The label of each field, translated where drawn. @i18n-keys */
const WEIGHT_LABEL = {
  priority_critical: "Critical priority",
  priority_high: "High priority",
  priority_medium: "Medium priority",
  priority_low: "Low priority",
  kev: "KEV",
  crown_jewel: "Crown jewel touched",
  overdue: "Overdue",
  due_soon: "Due soon",
  due_soon_days: "Due soon within (days)",
  no_owner: "No owner",
  age_per_day: "Per day open",
  age_max: "Age cap",
} as const satisfies Record<Key, string>;

/** The helper line of each field, translated where drawn. @i18n-keys */
const WEIGHT_HELPER = {
  priority_critical: "Points for a critical case",
  priority_high: "Points for a high case",
  priority_medium: "Points for a medium case",
  priority_low: "Points for a low case",
  kev: "Points when the case holds a KEV issue",
  crown_jewel: "Points when the case touches a crown jewel",
  overdue: "Points when the due date or the SLA has passed",
  due_soon: "Points when the due date is near",
  due_soon_days: "How near the due date is, from 1 to 90 days",
  no_owner: "Points when nobody owns the case",
  age_per_day: "Points for each day the case is open",
  age_max: "The most points the age adds",
} as const satisfies Record<Key, string>;

/** Each field in display order with its bounds (backend CaseAttentionWeights). */
export const WEIGHT_FIELDS: { key: Key; label: string; helper: string; min: number; max: number }[] = (
  Object.keys(WEIGHT_LABEL) as Key[]
).map((key) => ({
  key,
  label: WEIGHT_LABEL[key],
  helper: WEIGHT_HELPER[key],
  min: key === "due_soon_days" ? 1 : 0,
  max: key === "due_soon_days" ? 90 : 100,
}));

/** A typed value: a whole number within the field's bounds, else null. */
export function parseWeight(text: string, min: number, max: number): number | null {
  const trimmed = text.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return value >= min && value <= max ? value : null;
}

function asText(p: CaseAttentionProfile): Record<Key, string> {
  return Object.fromEntries(Object.entries(p).map(([k, v]) => [k, String(v)])) as Record<Key, string>;
}

export function CaseAttentionWeightsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data } = useOrganizationContext();
  const save = useSaveOrganizationContext();
  const current = weightsToProfile(weightsFromProfile(data?.profile?.case_attention));
  const [text, setText] = useState<Record<Key, string> | null>(null);
  const values = text ?? asText(current);
  const parsed = Object.fromEntries(WEIGHT_FIELDS.map((f) => [f.key, parseWeight(values[f.key], f.min, f.max)])) as Record<Key, number | null>;
  const valid = WEIGHT_FIELDS.every((f) => parsed[f.key] !== null);

  const close = () => {
    setText(null);
    onClose();
  };
  const submit = () => {
    if (!valid || !data?.profile) return;
    save.mutate(
      { profile: { ...data.profile, case_attention: parsed as CaseAttentionProfile }, source: "form" },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Ranking points saved"), variant: "success" });
          close();
        },
        onError: (err) => toast({ title: t_i18n("The ranking points were not saved"), description: err.message, variant: "error" }),
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) close(); }}>
      <DialogContent size="md" data-testid="ctem-cases-attention-weights">
        <DialogHeader>
          <DialogTitle>{t_i18n("Ranking points")}</DialogTitle>
          <DialogDescription>
            {t_i18n("How many points each reason adds to the attention score of a case. Cases with the most points come first, for everyone.")}
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <form
            id="ctem-cases-attention-weights-form"
            className="grid grid-cols-1 gap-4 sm:grid-cols-2"
            onSubmit={(e) => {
              e.preventDefault();
              submit();
            }}
          >
            {WEIGHT_FIELDS.map((f) => {
              const id = `ctem-attention-${f.key}`;
              const invalid = parsed[f.key] === null;
              return (
                <SettingsField
                  key={f.key}
                  id={id}
                  label={t_i18n(f.label)}
                  helper={t_i18n(f.helper)}
                  error={invalid ? t_i18n("Enter a whole number from {min} to {max}", { min: f.min, max: f.max }) : undefined}
                >
                  <Input
                    id={id}
                    inputMode="numeric"
                    value={values[f.key]}
                    aria-invalid={invalid}
                    aria-labelledby={settingsFieldLabelId(id)}
                    aria-describedby={settingsFieldHintId(id)}
                    onChange={(e) => setText({ ...values, [f.key]: e.target.value })}
                    data-testid={`${id}-input`}
                  />
                </SettingsField>
              );
            })}
          </form>
        </DialogBody>
        <DialogFooter>
          <Button
            type="button"
            variant="ghost"
            onClick={() => setText(asText(weightsToProfile(DEFAULT_CASE_ATTENTION_WEIGHTS)))}
            data-testid="ctem-cases-attention-weights-reset"
          >
            {t_i18n("Reset to defaults")}
          </Button>
          <Button type="button" variant="outline" onClick={close}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            type="submit"
            form="ctem-cases-attention-weights-form"
            disabled={!valid || save.isPending}
            data-testid="ctem-cases-attention-weights-save"
          >
            {t_i18n("Save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
