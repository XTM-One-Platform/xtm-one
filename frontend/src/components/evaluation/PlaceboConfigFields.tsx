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

import { AlertTriangle, X } from "lucide-react";
import { Input } from "@/components/ui/input";
import { InfoTip } from "@/components/ui/info-tip";
import { Tip } from "@/components/ui/tooltip";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Switch } from "@/components/ui/switch";
import { useFormatter } from "@/i18n";
import { EVALUATION_TERMS } from "@/lib/evaluation-glossary";
import { cn } from "@/lib/utils";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import {
  MAX_CUSTOM_TEXT,
  MAX_TRIALS_PER_ENTRY,
  PLACEBO_DRAFT_ERRORS,
  placeboDraftError,
  type PlaceboDraft,
} from "@/lib/placebo";

/**
 * The placebo control's concepts, as `dev-docs/agent-placebo-evaluation.md` and
 * `services/evaluation/placebo_arms.py` define them: shared with `PlaceboConfigCard`'s
 * read view. @i18n-keys
 */
const PLACEBO_FIELD_NOTES = {
  control:
    "Compares each agent with copies of itself whose persona, Prompts or skills are replaced by neutral text of the same length, to see whether those instructions earn their place.",
  targets:
    "The parts of each agent the placebo replaces. Each target gets its own placebo arm; an agent without the target gets no arm for it.",
} as const;

/** The "i" explaining one placebo concept, for this form and `PlaceboConfigCard`'s read view. */
export function PlaceboNoteTip({ note }: { note: keyof typeof PLACEBO_FIELD_NOTES | "noTargetArm" }) {
  const { t_i18n } = useFormatter();
  const labels = {
    control: t_i18n("What a placebo control compares"),
    targets: t_i18n("What a target is"),
    noTargetArm: t_i18n("What an arm is"),
  };
  // What an arm is, shared with the placebo comparison pages.
  const content = note === "noTargetArm" ? EVALUATION_TERMS.placeboArm : PLACEBO_FIELD_NOTES[note];
  return <InfoTip content={t_i18n(content)} label={labels[note]} />;
}

/**
 * The fields of a placebo control (FR-1 to FR-4b): which targets the placebo
 * replaces, trials per entry, the no-target arm and an optional custom text.
 * Controlled; the rule the draft breaks shows under the fields.
 */
export function PlaceboConfigFields({
  draft,
  onChange,
  executionMode,
  skills,
}: {
  draft: PlaceboDraft;
  onChange: (draft: PlaceboDraft) => void;
  executionMode: string;
  /** The skills a single-skill target may name. */
  skills: { id: string; name: string }[];
}) {
  const { t_i18n } = useFormatter();
  const fullLoop = executionMode === "full_loop";
  const set = (patch: Partial<PlaceboDraft>) => onChange({ ...draft, ...patch });
  const error = placeboDraftError(draft, executionMode);
  const skillName = (id: string) => skills.find((skill) => skill.id === id)?.name ?? id;
  const skillOptions = skills
    .filter((skill) => !draft.skillIds.includes(skill.id))
    .map((skill) => ({ value: skill.id, label: skill.name }));

  // Why a target cannot be turned on, said on the disabled switch itself: the
  // rule the draft would break, worded as the save's refusal is.
  const lockReason = (blockedBySkills: boolean): string | undefined =>
    !fullLoop ? t_i18n(PLACEBO_DRAFT_ERRORS.needs_full_loop) : blockedBySkills ? t_i18n(PLACEBO_DRAFT_ERRORS.skills_and_skill) : undefined;

  const targetRow = (
    key: "persona" | "prompts" | "skills",
    title: string,
    description: string,
    disabled: boolean,
    reason?: string,
  ) => (
    <div className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
      <div className={cn(disabled && "opacity-60")}>
        <p className="content-base font-medium">{title}</p>
        <p className="mt-0.5 text-[11px] text-default-secondary">{description}</p>
      </div>
      <Tip content={disabled ? reason : undefined} wrap="auto">
        <Switch aria-label={title} checked={draft[key]} disabled={disabled} onCheckedChange={(checked) => set({ [key]: checked })} />
      </Tip>
    </div>
  );

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <p className="flex items-center gap-1 content-caption font-medium uppercase tracking-wider text-default-secondary">
          {t_i18n("Targets")}
          <PlaceboNoteTip note="targets" />
        </p>
        {targetRow("persona", t_i18n("Persona"), t_i18n("The whole persona, replaced by a neutral text of the same length."), false)}
        {targetRow(
          "prompts",
          t_i18n("Attached Prompts"),
          t_i18n("Every attached Prompt, each replaced by a neutral Prompt of the same length."),
          !fullLoop && !draft.prompts,
          lockReason(false),
        )}
        {targetRow(
          "skills",
          t_i18n("All skills"),
          t_i18n("Every bound skill at once, each replaced by a neutral skill of the same length."),
          (!fullLoop && !draft.skills) || draft.skillIds.length > 0,
          lockReason(draft.skillIds.length > 0),
        )}
        <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
          <p className={cn("content-base font-medium", (!fullLoop || draft.skills) && "opacity-60")}>{t_i18n("Single skills")}</p>
          <p className={cn("mt-0.5 text-[11px] text-default-secondary", (!fullLoop || draft.skills) && "opacity-60")}>
            {t_i18n("One target per skill; an agent the skill is not bound to gets no arm for it.")}
          </p>
          {draft.skillIds.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {draft.skillIds.map((id) => (
                <span key={id} className="inline-flex items-center gap-1 rounded-sm border border-accent/20 bg-accent/10 px-1.5 py-0.5 content-caption text-accent">
                  {skillName(id)}
                  <button
                    type="button"
                    aria-label={t_i18n("Remove {name}", { name: skillName(id) })}
                    className="hover:text-default-primary"
                    onClick={() => set({ skillIds: draft.skillIds.filter((skillId) => skillId !== id) })}
                  >
                    <X className="h-2.5 w-2.5" />
                  </button>
                </span>
              ))}
            </div>
          )}
          <Tip content={lockReason(draft.skills)} wrap={!fullLoop || draft.skills} wrapClassName="mt-2 block">
          <div className={cn(fullLoop && !draft.skills && "mt-2")}>
            <SearchableSelect
              aria-label={t_i18n("Add a skill")}
              options={skillOptions}
              value={null}
              onValueChange={(id) => set({ skillIds: [...draft.skillIds, id] })}
              placeholder={t_i18n("Add a skill")}
              emptyMessage={t_i18n("No skills found")}
              disabled={!fullLoop || draft.skills}
            />
          </div>
          </Tip>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label htmlFor="placebo-trials" className="mb-1 block content-caption font-medium uppercase tracking-wider text-default-secondary">
            {t_i18n("Trials per entry")}
          </label>
          <Input
            id="placebo-trials"
            aria-label={t_i18n("Trials per entry")}
            type="number"
            min={1}
            max={MAX_TRIALS_PER_ENTRY}
            step={1}
            className="h-9"
            value={Number.isNaN(draft.trialsPerEntry) ? "" : draft.trialsPerEntry}
            onChange={(e) => set({ trialsPerEntry: e.target.value === "" ? Number.NaN : Number(e.target.value) })}
          />
          <p className="mt-1 text-[11px] text-default-secondary">{t_i18n("Each entry runs this many times in every arm (1-30).")}</p>
        </div>
        <div className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
          <div>
            <p className="flex items-center gap-1 content-base font-medium">
              {t_i18n("No-target arm")}
              <PlaceboNoteTip note="noTargetArm" />
            </p>
            <p className="mt-0.5 text-[11px] text-default-secondary">{t_i18n("Also run the agent with each target removed.")}</p>
          </div>
          <Switch aria-label={t_i18n("No-target arm")} checked={draft.includeNoTarget} onCheckedChange={(checked) => set({ includeNoTarget: checked })} />
        </div>
      </div>

      <div>
        <label htmlFor="placebo-custom-text" className="mb-1 block content-caption font-medium uppercase tracking-wider text-default-secondary">
          {t_i18n("Custom placebo text (optional)")}
        </label>
        <textarea
          id="placebo-custom-text"
          value={draft.customText}
          onChange={(e) => set({ customText: e.target.value })}
          rows={3}
          placeholder={t_i18n("Leave empty to use the neutral corpus shipped with the platform.")}
          className={`w-full resize-y rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 content-base placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
        />
        <p className="mt-1 text-[11px] text-default-secondary">
          {t_i18n("{count, number} / {max, number} characters. Matched to each original's length; it must share no distinctive word with what it replaces.", {
            count: draft.customText.length,
            max: MAX_CUSTOM_TEXT,
          })}
        </p>
      </div>

      {error && (
        <div role="alert" className="flex items-start gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.06] px-3 py-2">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" />
          <p className="content-compact text-feedback-warning-primary">{t_i18n(PLACEBO_DRAFT_ERRORS[error])}</p>
        </div>
      )}
    </div>
  );
}
