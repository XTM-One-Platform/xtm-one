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
 * "Apply a preset" of the Members view (dev-docs/ctem-fleets.md 10.6, R4 of
 * #9162): one dialog for the fleet defaults and for the selected rows. The
 * picker lists the built-in presets first, then the company's (runtime data,
 * so the shared `SearchableSelect`); the chosen preset's values are shown
 * before Apply, and the caller writes them through the routes it already
 * uses (`PATCH /fleets/{id}` or `POST /fleets/{id}/members/bulk`) with its
 * Undo. A preset writes values and keeps no link to the fleet.
 */
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { TagList } from "@/components/ui/tag-list";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import type { FleetMemberPreset } from "@/hooks/api/useFleetApi";
import type { Organization } from "@/hooks/api/useOrganizationsApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import { CONTEXT_INCLUSION_LABELS } from "./context-inclusion";
import { MEMBER_SETTING_LABELS, firstLine, presetName, presetSettings, scopeTags } from "./members-view";

/** The group headers of the picker. @i18n-keys */
const PRESET_GROUPS = { builtin: "Built-in", company: "Company presets" } as const;

export function ApplyPresetDialog({
  open,
  onOpenChange,
  presets,
  loading,
  organizations,
  description,
  onApply,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  presets: readonly FleetMemberPreset[];
  loading: boolean;
  organizations: readonly Organization[];
  /** Who the preset applies to, translated. */
  description: string;
  onApply: (preset: FleetMemberPreset) => void;
}) {
  const { t_i18n } = useFormatter();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t_i18n("Apply a preset")}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {/* Mounted per opening: no preset is picked when the dialog opens. */}
        {open && (
          <PresetForm
            presets={presets}
            loading={loading}
            organizations={organizations}
            onCancel={() => onOpenChange(false)}
            onApply={onApply}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

function PresetForm({
  presets,
  loading,
  organizations,
  onCancel,
  onApply,
}: {
  presets: readonly FleetMemberPreset[];
  loading: boolean;
  organizations: readonly Organization[];
  onCancel: () => void;
  onApply: (preset: FleetMemberPreset) => void;
}) {
  const { t_i18n } = useFormatter();
  const [presetId, setPresetId] = useState<string>("");
  const chosen = presets.find((p) => p.id === presetId);
  const nameOf = (preset: FleetMemberPreset) => {
    const name = presetName(preset);
    return name.translate ? t_i18n(name.text) : name.text;
  };
  const options = presets.map((preset) => ({
    value: preset.id,
    label: nameOf(preset),
    group: t_i18n(preset.builtin ? PRESET_GROUPS.builtin : PRESET_GROUPS.company),
  }));

  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (chosen) onApply(chosen);
      }}
    >
      <div className="space-y-1.5">
        <Label htmlFor="apply-preset-picker">{t_i18n("Preset")}</Label>
        <SearchableSelect
          id="apply-preset-picker"
          aria-label={t_i18n("Preset")}
          aria-describedby="apply-preset-help"
          options={options}
          // The built-ins keep the order of 10.6, before the company's presets.
          sort={false}
          searchable="auto"
          value={presetId}
          onValueChange={setPresetId}
          disabled={loading}
          placeholder={loading ? t_i18n("Loading...") : t_i18n("Choose a preset")}
          emptyMessage={t_i18n("No match")}
          className="w-full"
        />
        <p id="apply-preset-help" className="content-compact text-default-secondary">
          {t_i18n("A preset writes its values once; changing the preset later changes no fleet")}
        </p>
      </div>
      {chosen && <PresetValues preset={chosen} organizations={organizations} />}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="secondary" onClick={onCancel}>
          {t_i18n("Cancel")}
        </Button>
        <Button type="submit" disabled={!chosen}>
          {t_i18n("Apply")}
        </Button>
      </div>
    </form>
  );
}

/**
 * What the chosen preset writes, one line per setting it sets. No value is cut out of reach
 * (UX round 1 of #9422, B2): the organizations are the scope tags of the defaults bar ("+N" with
 * a keyboard and touch tooltip), the instruction wraps to 2 lines and its whole text is a
 * focusable tooltip.
 */
export function PresetValues({
  preset,
  organizations,
  badgeMin = "whole",
  lines,
}: {
  preset: Pick<FleetMemberPreset, "values">;
  organizations: readonly Organization[];
  /**
   * How the organization badges shrink: `whole` (Apply a preset) keeps the first
   * name whole; `short` lets it truncate with its tooltip, so a table column
   * shrinks to its box (Settings > Member presets, UX round 2 of #9447, B1).
   */
  badgeMin?: "short" | "whole";
  /**
   * At most this many settings, one line each; the rest sit behind a "+N" marker whose
   * focusable tooltip names them (a two-line table row, Settings > Member presets, UX round 5
   * of #10980, B2). Unset: every setting, the instruction on 2 lines (Apply a preset).
   */
  lines?: number;
}) {
  const { t_i18n } = useFormatter();
  const values = preset.values;
  const settings = presetSettings(values);
  const shown = lines ? settings.slice(0, lines) : settings;
  const hidden = settings.slice(shown.length);
  const valueText = (setting: (typeof settings)[number]) =>
    setting === "context_inclusion"
      ? t_i18n(CONTEXT_INCLUSION_LABELS[values.context_inclusion ?? "none"])
      : setting === "organization_scope"
        ? scopeTags(values.organization_scope, organizations).map((tag) => (tag.translate ? t_i18n(tag.text) : tag.text)).join(", ")
        : (values.collaboration_instruction ?? "");
  const hiddenText = hidden.map((setting) => `${t_i18n(MEMBER_SETTING_LABELS[setting])}: ${valueText(setting)}`).join("; ");
  const list = (
    <dl className={cn("space-y-1 content-compact", hidden.length > 0 && "min-w-0 flex-1")} data-testid="apply-preset-values">
      {shown.map((setting) => (
        <div key={setting} className="flex min-w-0 items-start gap-2">
          <dt className="shrink-0 text-default-secondary">{t_i18n(MEMBER_SETTING_LABELS[setting])}</dt>
          <dd className="min-w-0 flex-1">
            {setting === "context_inclusion" ? (
              t_i18n(CONTEXT_INCLUSION_LABELS[values.context_inclusion ?? "none"])
            ) : setting === "organization_scope" ? (
              // A flex row sizes the tag list to its content, as in the defaults bar: "+N" sits right after
              // the last chip instead of at the far end of the dd (UX round 2 of #9422, B3).
              <div className="flex min-w-0">
                <TagList
                  tags={scopeTags(values.organization_scope, organizations).map((tag) => (tag.translate ? t_i18n(tag.text) : tag.text))}
                  max={2}
                  badgeMin={badgeMin}
                  className="min-w-0"
                />
              </div>
            ) : (
              <InstructionValue text={values.collaboration_instruction ?? ""} clamp={lines ? 1 : 2} />
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
  if (hidden.length === 0) return list;
  return (
    <div className="flex min-w-0 items-end gap-1">
      {list}
      {/* The settings past the row's lines, reachable by keyboard and touch, as the tag list's marker. */}
      <Tip content={hiddenText}>
        <span
          tabIndex={0}
          role="note"
          aria-label={hiddenText}
          data-testid="apply-preset-values-more"
          className="content-caption shrink-0 whitespace-nowrap rounded-sm text-default-secondary outline-none focus-visible:ring-2 focus-visible:ring-focus"
        >
          +{hidden.length}
        </span>
      </Tip>
    </div>
  );
}

/** A collaboration instruction on at most `clamp` lines; the whole text is a tooltip a keyboard reaches. */
function InstructionValue({ text, clamp }: { text: string; clamp: 1 | 2 }) {
  const shown = firstLine(text);
  return (
    <Tip content={text}>
      <span tabIndex={0} className={cn("block break-words", clamp === 1 ? "line-clamp-1" : "line-clamp-2", TIP_WRAP_FOCUS_RING)} data-testid="apply-preset-instruction">
        {shown}
      </span>
    </Tip>
  );
}
