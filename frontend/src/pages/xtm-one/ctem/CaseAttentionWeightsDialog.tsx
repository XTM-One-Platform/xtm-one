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
import {
  SettingsField,
  SettingsGroupDescription,
  SettingsGroupTitle,
  settingsFieldHintId,
  settingsFieldLabelId,
} from "@/components/ui/settings-form";
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
  crown_jewel: "Primary asset touched",
  overdue: "Overdue",
  due_soon: "Due soon",
  due_soon_days: "Due soon within (days)",
  no_owner: "No owner",
  age_per_day: "Per day the case is open",
  age_max: "Age cap",
} as const satisfies Record<Key, string>;

/** The helper line of each field, translated where drawn. @i18n-keys */
const WEIGHT_HELPER = {
  priority_critical: "Points for a critical priority case",
  priority_high: "Points for a high priority case",
  priority_medium: "Points for a medium priority case",
  priority_low: "Points for a low priority case",
  kev: "Points when the case holds a KEV issue",
  crown_jewel: "Points when the case touches a primary asset",
  overdue: "Points when the due date or the SLA has passed",
  due_soon: "Points when the due date is near",
  due_soon_days: "How near the due date is, from 1 to 90 days",
  no_owner: "Points when nobody owns the case",
  age_per_day: "Points for each day the case is open",
  age_max: "The most points the age adds",
} as const satisfies Record<Key, string>;

/** The info bubble of each field: what it adds and its unit, translated where drawn. @i18n-keys */
const WEIGHT_INFO = {
  priority_critical: "Points added to the score of a case whose priority is critical. A case gets the points of one priority only.",
  priority_high: "Points added to the score of a case whose priority is high. A case gets the points of one priority only.",
  priority_medium: "Points added to the score of a case whose priority is medium. A case gets the points of one priority only.",
  priority_low: "Points added to the score of a case whose priority is low. A case gets the points of one priority only.",
  kev: "Points added when one issue of the case is in the CISA Known Exploited Vulnerabilities catalog.",
  crown_jewel: "Points added when the case touches an asset of a primary asset declared in the organization profile.",
  overdue: "Points added when the due date or the SLA of the case has passed.",
  due_soon: "Points added when the due date falls within the days set in Due soon within.",
  due_soon_days: "A number of days, from 1 to 90: how close the due date must be for Due soon to add its points. With 3, a case due in 2 days gets them, a case due in 5 days does not.",
  no_owner: "Points added when nobody owns the case.",
  age_per_day: "Points added for each day the case has been open, until the age cap is reached.",
  age_max: "The most points the age can add. With 1 per day and a cap of 10, a case open 30 days gets 10.",
} as const satisfies Record<Key, string>;

type GroupId = "priority" | "signals" | "due" | "owner_age";

/** The title of each group, translated where drawn. @i18n-keys */
const GROUP_TITLE = {
  priority: "Priority",
  signals: "Signals",
  due: "Due date",
  owner_age: "Owner and age",
} as const satisfies Record<GroupId, string>;

/** The one-line description of each group, translated where drawn. @i18n-keys */
const GROUP_DESCRIPTION = {
  priority: "Points by the priority of the case.",
  signals: "Points for what the case holds.",
  due: "Points when the due date is near or has passed.",
  owner_age: "Points for a case nobody owns and for each day it stays open.",
} as const satisfies Record<GroupId, string>;

const GROUP_KEYS: Record<GroupId, readonly Key[]> = {
  priority: ["priority_critical", "priority_high", "priority_medium", "priority_low"],
  signals: ["kev", "crown_jewel"],
  due: ["overdue", "due_soon", "due_soon_days"],
  owner_age: ["no_owner", "age_per_day", "age_max"],
};

/** The groups of the dialog, in display order. */
const WEIGHT_GROUPS = (Object.keys(GROUP_TITLE) as GroupId[]).map((id) => ({
  id,
  title: GROUP_TITLE[id],
  description: GROUP_DESCRIPTION[id],
  keys: GROUP_KEYS[id],
}));

/** Each field in display order with its bounds (backend CaseAttentionWeights). */
export const WEIGHT_FIELDS: { key: Key; label: string; helper: string; info: string; min: number; max: number }[] = WEIGHT_GROUPS.flatMap(
  (g) => g.keys,
).map((key) => ({
  key,
  label: WEIGHT_LABEL[key],
  helper: WEIGHT_HELPER[key],
  info: WEIGHT_INFO[key],
  min: key === "due_soon_days" ? 1 : 0,
  max: key === "due_soon_days" ? 90 : 100,
}));

export { WEIGHT_GROUPS };

/** The info bubble: the field's own text, then the example, where it is read and what 0 does. */
function WeightInfo({ field }: { field: (typeof WEIGHT_FIELDS)[number] }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-2" data-testid={`ctem-attention-${field.key}-info`}>
      <p>{t_i18n(field.info)}</p>
      <p>{t_i18n("Example with the default points: a critical priority case with a KEV issue and no owner scores 40 + 15 + 5 = 60.")}</p>
      <p>{t_i18n("The score is read by the Attention column and its sort on Cases, by Act on this first and by Why this case needs attention now on the case page.")}</p>
      {field.key !== "due_soon_days" && (
        <p>{t_i18n("0 removes this reason from the score and from the why line.")}</p>
      )}
    </div>
  );
}

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
            className="space-y-6"
            onSubmit={(e) => {
              e.preventDefault();
              submit();
            }}
          >
            {WEIGHT_GROUPS.map((g) => (
              <section key={g.id} aria-labelledby={`ctem-attention-group-${g.id}`} data-testid={`ctem-attention-group-${g.id}`}>
                <SettingsGroupTitle id={`ctem-attention-group-${g.id}`}>{t_i18n(g.title)}</SettingsGroupTitle>
                <SettingsGroupDescription>{t_i18n(g.description)}</SettingsGroupDescription>
                <div className="mt-3 grid grid-cols-1 gap-4 sm:grid-cols-2">
                  {WEIGHT_FIELDS.filter((f) => g.keys.includes(f.key)).map((f) => {
                    const id = `ctem-attention-${f.key}`;
                    const invalid = parsed[f.key] === null;
                    return (
                      <SettingsField
                        key={f.key}
                        id={id}
                        label={t_i18n(f.label)}
                        helper={t_i18n(f.helper)}
                        info={<WeightInfo field={f} />}
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
                </div>
              </section>
            ))}
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
