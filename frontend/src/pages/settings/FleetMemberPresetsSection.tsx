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
 * Settings > Agents and automations > Fleets > Member presets (R4 of #9162,
 * dev-docs/ctem-fleets.md 10.6): the presets "Apply a preset" offers in every
 * fleet's Members view. The three built-ins are listed read only; the
 * company's presets are created, edited and deleted here by administrators
 * (decision R-D3 A, no personal preset). A preset writes its values once when
 * it is applied, so editing or deleting one changes no fleet.
 */
import { useState } from "react";
import { Layers, Pencil, Plus, Trash2 } from "lucide-react";
import { Textarea } from "@filigran/design-system";

import { PresetValues } from "@/components/fleets/ApplyPresetDialog";
import { ContextSelect } from "@/components/fleets/FleetMembersView";
import { OrganizationScopeDialog } from "@/components/fleets/OrganizationScopeDialog";
import { fleetRefusalMessage } from "@/components/fleets/fleet-refusals";
import {
  MEMBER_SETTING_LABELS,
  PRESET_NAME_MAX_LENGTH,
  type PresetDraft,
  presetDraft,
  presetDraftError,
  presetDraftValues,
  presetName,
  scopeTags,
} from "@/components/fleets/members-view";
import { BuiltinBadge } from "@/components/ui/builtin-badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { InfoTip } from "@/components/ui/info-tip";
import { Input } from "@/components/ui/input";
import { SettingsCard, SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { Switch } from "@/components/ui/switch";
import { TagList } from "@/components/ui/tag-list";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import {
  type FleetMemberPreset,
  type FleetMemberSetting,
  useCreateFleetMemberPreset,
  useDeleteFleetMemberPreset,
  useFleetMemberPresets,
  useUpdateFleetMemberPreset,
} from "@/hooks/api/useFleetApi";
import { type Organization, useOrganizations } from "@/hooks/api/useOrganizationsApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";

/** What each setting's switch says the preset does when it is on. @i18n-keys */
const SETTING_HELP = {
  context_inclusion: "What a connected member receives from the conversation that calls it",
  organization_scope: "The organizations a member reasons about, on every member",
  collaboration_instruction: "When and how the fleet works with a connected member",
} as const satisfies Record<FleetMemberSetting, string>;

/**
 * Each setting's info bubble (Design standards s.2, UX round 3 B1 on #9447):
 * what the value is, a real example, where applying the preset writes it, and
 * what happens when its switch is off. @i18n-keys
 */
const SETTING_INFO = {
  context_inclusion:
    "What a connected member receives from the conversation that calls it. For example, A summary: the member gets a short summary of the conversation, not the transcript. Applying the preset writes it on the fleet defaults or on the selected members. When the switch is off, the preset does not write it and the fleet keeps its current value.",
  organization_scope:
    "The organizations a member reasons about, on every member. For example, Own organization: the member works only on the organization of the person who asks. Applying the preset writes it on the fleet defaults or on the selected members. When the switch is off, the preset does not write it and the fleet keeps its current value.",
  collaboration_instruction:
    "The instruction the fleet follows when it works with a connected member. For example: Ask the regional SOC for its triage before you answer. Applying the preset writes it on the fleet defaults or on the selected members, and an empty instruction writes none. When the switch is off, the preset does not write it and the fleet keeps its current value.",
} as const satisfies Record<FleetMemberSetting, string>;

/** The presets table's keys in the URL: its Type filter is `?presets_f_type=builtin`. */
const PRESETS_URL_KEY = "presets_";

export function FleetMemberPresetsSection() {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const { data: presets = [], isLoading } = useFleetMemberPresets();
  const { data: organizations = [] } = useOrganizations();
  const remove = useDeleteFleetMemberPreset();
  /** The preset being edited, `null` for a new one; `undefined` when the editor is closed. */
  const [editing, setEditing] = useState<FleetMemberPreset | null | undefined>(undefined);
  const nameOf = (preset: FleetMemberPreset) => {
    const name = presetName(preset);
    return name.translate ? t_i18n(name.text) : name.text;
  };

  const typeOf = (preset: FleetMemberPreset) => (preset.builtin ? t_i18n("Built-in") : t_i18n("Company"));
  // The Type filter is the table's own filter pill (UX round 2 of #9447, B3), kept in the URL.
  const filters: DataTableFilter<FleetMemberPreset>[] = [
    {
      key: "type",
      label: "Type",
      values: (preset) => [preset.builtin ? "builtin" : "company"],
      options: [
        { value: "builtin", label: t_i18n("Built-in") },
        { value: "company", label: t_i18n("Company") },
      ],
    },
  ];
  const columns: DataTableColumn<FleetMemberPreset>[] = [
    {
      key: "name",
      header: "Name",
      sortValue: nameOf,
      cell: (preset) => (
        <div className="flex min-w-0 items-center gap-2" data-testid="settings-fleet-member-preset">
          <span className="truncate content-base font-medium text-default-primary" title={nameOf(preset)}>
            {nameOf(preset)}
          </span>
          {preset.builtin && (
            // The badge never shrinks or wraps; the name beside it truncates (UX round 2 of #9447, B2).
            <span className="shrink-0 whitespace-nowrap" data-testid="settings-fleet-member-preset-builtin">
              <BuiltinBadge size="sm" />
            </span>
          )}
        </div>
      ),
      className: "max-w-[16rem]",
    },
    // Steps aside below a 56rem table box (the Built-in badge and the Type filter already say it; the
    // default sort still reads it), so the actions stay in view at 1024 (UX round 2 of #9447, B1).
    { key: "type", header: "Type", sortValue: typeOf, cell: typeOf, className: "whitespace-nowrap text-default-secondary", hideBelow: "4xl" },
    {
      key: "values",
      header: "Values",
      hideable: true,
      // Bounded so the table fits its card at 1024 with the actions in view: the first organization
      // badge truncates with its tooltip and a long word breaks (UX round 2 of #9447, B1). Two settings
      // at most, one line each, the rest behind "+N", so every row is the two-line height (UX round 5 of #10980, B2).
      cell: (preset) => (
        <div className="min-w-0 max-w-[16rem] [overflow-wrap:anywhere]" data-testid="settings-fleet-member-preset-values">
          <PresetValues preset={preset} organizations={organizations} badgeMin="short" lines={2} />
        </div>
      ),
      className: "min-w-0",
    },
  ];
  if (isAdmin) {
    columns.push({
      key: "actions",
      header: "Actions",
      align: "right",
      cell: (preset) =>
        preset.builtin ? null : (
          <div className="flex items-center justify-end gap-1">
            <Tip content={t_i18n("Edit {name}", { name: preset.name })}>
              <Button type="button" variant="ghost" size="icon" aria-label={t_i18n("Edit {name}", { name: preset.name })} onClick={() => setEditing(preset)}>
                <Pencil className="h-4 w-4" aria-hidden />
              </Button>
            </Tip>
            <Tip content={t_i18n("Delete {name}", { name: preset.name })}>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={t_i18n("Delete {name}", { name: preset.name })}
                disabled={remove.isPending}
                onClick={() => void deletePreset(preset)}
              >
                <Trash2 className="h-4 w-4" aria-hidden />
              </Button>
            </Tip>
          </div>
        ),
    });
  }

  const deletePreset = async (preset: FleetMemberPreset) => {
    const ok = await confirm({
      title: t_i18n("Delete the preset {name}?", { name: preset.name }),
      description: t_i18n("It is no longer offered in Apply a preset. Fleets keep the values it wrote."),
      confirmLabel: t_i18n("Delete"),
    });
    if (!ok) return;
    remove.mutate(preset.id, {
      onSuccess: () => toast({ title: t_i18n("Preset deleted"), variant: "success" }),
      onError: (err) => toast({ title: t_i18n("The preset was not deleted"), description: fleetRefusalMessage(err, t_i18n), variant: "error" }),
    });
  };

  return (
    <SettingsCard
      data-testid="settings-fleet-member-presets"
      icon={Layers}
      title={t_i18n("Member presets")}
      description={t_i18n("The presets Apply a preset offers in every fleet's Members view. A preset writes its values once; changing it later changes no fleet.")}
      action={
        isAdmin && (
          <Button type="button" variant="secondary" size="sm" className="gap-1.5" onClick={() => setEditing(null)}>
            <Plus className="h-4 w-4" aria-hidden />
            {t_i18n("New preset")}
          </Button>
        )
      }
    >
      {ConfirmEl}
      <DataTable
        rows={presets}
        columns={columns}
        rowKey={(preset) => preset.id}
        storageKey="settings-fleet-member-presets"
        rowLines={2}
        defaultSort={{ key: "type", direction: "asc" }}
        searchFields={(preset) => [nameOf(preset)]}
        searchPlaceholder={t_i18n("Search the presets by name")}
        isLoading={isLoading}
        filters={filters}
        urlKey={PRESETS_URL_KEY}
        emptyLabel={t_i18n("No preset")}
      />
      {!isLoading && !presets.some((p) => !p.builtin) && (
        <p className="mt-3 content-compact text-default-secondary" data-testid="settings-fleet-member-presets-empty">
          {isAdmin
            ? t_i18n("No company preset yet: New preset adds one beside the built-ins.")
            : t_i18n("No company preset yet: an administrator adds them here.")}
        </p>
      )}
      <PresetEditorDialog
        preset={editing}
        presets={presets}
        organizations={organizations}
        onClose={() => setEditing(undefined)}
      />
    </SettingsCard>
  );
}

/** Create or edit a company preset: its name and the settings it writes, at least one. */
function PresetEditorDialog({
  preset,
  presets,
  organizations,
  onClose,
}: {
  /** `null` creates, a preset edits, `undefined` is closed. */
  preset: FleetMemberPreset | null | undefined;
  presets: readonly FleetMemberPreset[];
  organizations: readonly Organization[];
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const open = preset !== undefined;
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{preset ? t_i18n("Edit the preset") : t_i18n("New preset")}</DialogTitle>
          <DialogDescription>{t_i18n("Turn on the settings the preset writes; the others keep their value when it is applied.")}</DialogDescription>
        </DialogHeader>
        {/* Mounted per opening: the draft starts from the preset given each time. */}
        {open && <PresetEditorForm preset={preset} presets={presets} organizations={organizations} onClose={onClose} />}
      </DialogContent>
    </Dialog>
  );
}

function PresetEditorForm({
  preset,
  presets,
  organizations,
  onClose,
}: {
  preset: FleetMemberPreset | null;
  presets: readonly FleetMemberPreset[];
  organizations: readonly Organization[];
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const create = useCreateFleetMemberPreset();
  const update = useUpdateFleetMemberPreset();
  const saving = create.isPending || update.isPending;
  const [draft, setDraft] = useState<PresetDraft>(() => presetDraft(preset ?? undefined));
  const [submitted, setSubmitted] = useState(false);
  const [scopeDialog, setScopeDialog] = useState(false);
  const error = presetDraftError(draft, presets, preset?.id);
  const shownError = submitted ? error : null;
  const change = (patch: Partial<PresetDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const toggle = (setting: FleetMemberSetting, on: boolean) => setDraft((d) => ({ ...d, sets: { ...d.sets, [setting]: on } }));

  const save = () => {
    setSubmitted(true);
    if (error) return;
    const input = { name: draft.name.trim(), values: presetDraftValues(draft) };
    const onError = (err: Error) =>
      toast({ title: t_i18n("The preset was not saved"), description: fleetRefusalMessage(err, t_i18n), variant: "error" });
    const onSuccess = () => {
      toast({ title: t_i18n("Preset saved"), variant: "success" });
      onClose();
    };
    if (preset) update.mutate({ id: preset.id, input }, { onSuccess, onError });
    else create.mutate(input, { onSuccess, onError });
  };

  const settingSwitch = (setting: FleetMemberSetting) => (
    <div className="flex items-center justify-between gap-3">
      <span className="flex min-w-0 items-center gap-1.5">
        <span id={`preset-${setting}-label`} className="content-base text-default-primary">
          {t_i18n(MEMBER_SETTING_LABELS[setting])}
        </span>
        <InfoTip
          content={<div className="max-w-xs content-compact">{t_i18n(SETTING_INFO[setting])}</div>}
          label={t_i18n("About {name}", { name: t_i18n(MEMBER_SETTING_LABELS[setting]) })}
        />
      </span>
      <Switch
        aria-label={t_i18n("Write {field}", { field: t_i18n(MEMBER_SETTING_LABELS[setting]) })}
        checked={draft.sets[setting]}
        onCheckedChange={(on) => toggle(setting, on)}
        data-testid={`preset-set-${setting}`}
      />
    </div>
  );

  return (
    <form
      className="min-w-0 space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        save();
      }}
    >
      <SettingsField
        id="preset-name"
        label={t_i18n("Name")}
        required
        helper={t_i18n("The name fleets see in Apply a preset; unique, {max} characters at most ({count}/{max})", {
          count: draft.name.length,
          max: PRESET_NAME_MAX_LENGTH,
        })}
        info={t_i18n(
          "The name of the preset, shown in Apply a preset in every fleet's Members view and in this list. It must be unique among the built-ins and the company presets, without regard to case.",
        )}
        error={shownError?.field === "name" ? t_i18n(shownError.message) : undefined}
      >
        <Input
          id="preset-name"
          aria-label={t_i18n("Name")}
          value={draft.name}
          aria-describedby={settingsFieldHintId("preset-name")}
          aria-invalid={shownError?.field === "name" || undefined}
          placeholder={t_i18n("For example: Regional SOC, summaries only")}
          // Capped here rather than with maxLength, which also draws the library's
          // 10 px counter: the helper line says the length at 12 px (UX R1 B2 on #9447).
          onChange={(e) => change({ name: e.target.value.slice(0, PRESET_NAME_MAX_LENGTH) })}
        />
      </SettingsField>

      <fieldset className="min-w-0 space-y-4" aria-describedby="preset-settings-hint">
        <legend className="sr-only">{t_i18n("Settings the preset writes")}</legend>
        <div className="space-y-1.5">
          {settingSwitch("context_inclusion")}
          {draft.sets.context_inclusion && (
            <ContextSelect
              value={draft.context_inclusion}
              onChange={(value) => change({ context_inclusion: value })}
              className="w-full"
              label={t_i18n(MEMBER_SETTING_LABELS.context_inclusion)}
            />
          )}
          <p className="content-compact text-default-secondary">{t_i18n(SETTING_HELP.context_inclusion)}</p>
        </div>

        <div className="space-y-1.5">
          {settingSwitch("organization_scope")}
          {draft.sets.organization_scope && (
            <span className="flex h-9 min-w-0 items-center gap-1" data-testid="preset-organizations">
              <TagList
                tags={scopeTags(draft.organization_scope, organizations).map((tag) => (tag.translate ? t_i18n(tag.text) : tag.text))}
                max={2}
                className="min-w-0 flex-1"
              />
              <Tip content={t_i18n("Choose the organizations")}>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="ml-auto h-8 w-8 shrink-0"
                  aria-label={t_i18n("Choose the organizations")}
                  onClick={() => setScopeDialog(true)}
                >
                  <Pencil className="h-3.5 w-3.5" aria-hidden />
                </Button>
              </Tip>
            </span>
          )}
          <p className="content-compact text-default-secondary">{t_i18n(SETTING_HELP.organization_scope)}</p>
        </div>

        <div className="space-y-1.5">
          {settingSwitch("collaboration_instruction")}
          {draft.sets.collaboration_instruction && (
            <Textarea
              id="preset-instruction"
              aria-labelledby="preset-collaboration_instruction-label"
              value={draft.collaboration_instruction}
              maxLength={4000}
              rows={3}
              placeholder={t_i18n("When and how the fleet works with its connected members (optional)")}
              onChange={(e) => change({ collaboration_instruction: e.target.value })}
            />
          )}
          <p className="content-compact text-default-secondary">{t_i18n(SETTING_HELP.collaboration_instruction)}</p>
        </div>
        <p
          id="preset-settings-hint"
          role={shownError?.field === "settings" ? "alert" : undefined}
          className={shownError?.field === "settings" ? "content-compact text-feedback-error-primary" : "sr-only"}
        >
          {shownError?.field === "settings" ? t_i18n(shownError.message) : t_i18n("Choose at least one setting the preset writes")}
        </p>
      </fieldset>

      <OrganizationScopeDialog
        open={scopeDialog}
        onOpenChange={setScopeDialog}
        scope={draft.organization_scope}
        organizations={organizations}
        description={t_i18n("The organizations the preset writes")}
        onApply={(scope) => {
          setScopeDialog(false);
          change({ organization_scope: scope });
        }}
      />

      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onClose} disabled={saving}>
          {t_i18n("Cancel")}
        </Button>
        <Button type="submit" disabled={saving} aria-busy={saving || undefined}>
          {t_i18n("Save")}
        </Button>
      </div>
    </form>
  );
}
