// SPDX-License-Identifier: Apache-2.0

/**
 * Add, update and bulk-update dialog of the CTEM Inventory's rows (#1367, point 2, Sam 23 Nov 2026: "more normal
 * management", as in OpenAEV). One dialog for the assets and the asset groups: Add creates a row entered by hand,
 * Update changes one row, the bulk form changes the selected rows. A field a platform sync sends is locked on its rows
 * (point 3): it renders disabled with the reason, and the backend refuses the same change.
 */
import { useState } from "react";
import { Textarea } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { SearchableSelect, type SelectOption } from "@/components/ui/searchable-select";
import { SettingsField, settingsFieldLabelId } from "@/components/ui/settings-form";
import { useToast } from "@/components/ui/toast";
import { useOrganizationContext, useSaveOrganizationContext, type OrganizationProfile } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { ASSET_CRITICALITY_LABELS, parseTypeValue, typeOptions, typeValue } from "@/lib/asset-taxonomy";
import { ENVIRONMENT_LABELS, EXPOSURE_LABELS } from "@/lib/organization-import-filters";
import type { AssetGroupRow, AssetRow } from "@/lib/organization-profile";
import {
  fieldEditable,
  nameProblem,
  newAssetGroupRow,
  newAssetRow,
  patchRows,
  type InventorySection,
} from "./inventory-entries";

/** What the dialog does: create a row, change one, or change the selected rows. */
export type EntryDialogMode = { kind: "add" } | { kind: "update"; index: number } | { kind: "bulk"; indexes: number[] };

/** The value of a bulk select that leaves the field as each row has it. */
const KEEP = "__keep__";
/** The value of a select for "no value" (a Select item cannot be the empty string). */
const NONE = "__none__";

type Draft = Record<string, string>;

const rowsOf = (profile: OrganizationProfile | undefined, section: InventorySection) =>
  ((profile as unknown as Record<string, unknown> | undefined)?.[section] ?? []) as (AssetRow | AssetGroupRow)[];

function draftOf(section: InventorySection, row: AssetRow | AssetGroupRow | undefined, bulk: boolean): Draft {
  if (bulk) return { criticality: KEEP, internet_facing: KEEP, environment: KEEP, group: KEEP, owner: "" };
  if (section === "asset_groups") {
    const g = (row as AssetGroupRow | undefined) ?? newAssetGroupRow({});
    return { name: g.name, description: g.description, environment: g.environment || NONE, internet_facing: g.internet_facing, owner: g.owner };
  }
  const a = (row as AssetRow | undefined) ?? newAssetRow({});
  return {
    name: a.name,
    type: typeValue(a.category, a.subcategory),
    criticality: a.criticality || "unknown",
    environment: a.environment || NONE,
    internet_facing: a.internet_facing,
    owner: a.owner,
    group: a.group || NONE,
  };
}

/** The patch a draft sets: a kept bulk field and an untouched bulk owner set nothing. */
function patchOf(section: InventorySection, draft: Draft, bulk: boolean): Record<string, string> {
  const out: Record<string, string> = {};
  const take = (key: string) => {
    const value = draft[key];
    if (value === undefined || value === KEEP || (bulk && key === "owner" && !value.trim())) return;
    out[key] = value === NONE ? "" : key === "owner" || key === "name" || key === "description" ? value.trim() : value;
  };
  const keys = section === "asset_groups" ? ["name", "description", "environment", "internet_facing", "owner"] : ["name", "criticality", "environment", "internet_facing", "owner", "group"];
  keys.forEach(take);
  if (section === "assets_exposure" && draft.type && !bulk) Object.assign(out, parseTypeValue(draft.type));
  return out;
}

interface InventoryEntryDialogProps {
  section: InventorySection;
  mode: EntryDialogMode | null;
  onOpenChange: (open: boolean) => void;
  /** Called once the profile is saved (the caller clears its selection). */
  onSaved?: () => void;
}

export function InventoryEntryDialog({ section, mode, onOpenChange, onSaved }: InventoryEntryDialogProps) {
  const open = mode !== null;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* Remounted per opening, so the draft starts from the row each time. */}
      {mode && <EntryForm key={JSON.stringify(mode)} section={section} mode={mode} onClose={() => onOpenChange(false)} onSaved={onSaved} />}
    </Dialog>
  );
}

function EntryForm({ section, mode, onClose, onSaved }: { section: InventorySection; mode: EntryDialogMode; onClose: () => void; onSaved?: () => void }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data } = useOrganizationContext();
  const save = useSaveOrganizationContext();
  const rows = rowsOf(data?.profile, section);
  const bulk = mode.kind === "bulk";
  const row = mode.kind === "update" ? rows[mode.index] : undefined;
  const [draft, setDraft] = useState<Draft>(() => draftOf(section, row, bulk));
  const [error, setError] = useState("");
  const set = (key: string) => (value: string) => setDraft((d) => ({ ...d, [key]: value }));
  const isGroup = section === "asset_groups";
  const groups = (rowsOf(data?.profile, "asset_groups") as AssetGroupRow[]).map((g) => g.name).filter((n) => n.trim());
  // A field the row's source sends is locked on that row (the bulk form skips the rows it cannot change).
  const locked = (key: string) => mode.kind === "update" && Boolean(row) && !fieldEditable(section, row!, key);
  const lockedHelper = t_i18n("Set by the source that syncs this row.");

  const title =
    mode.kind === "add"
      ? t_i18n(isGroup ? "Add an asset group" : "Add an asset")
      : mode.kind === "update"
        ? t_i18n(isGroup ? "Update the asset group" : "Update the asset")
        : t_i18n(isGroup ? "Update the selected asset groups" : "Update the selected assets");

  const onSubmit = () => {
    if (!data) return;
    const patch = patchOf(section, draft, bulk);
    if (!bulk && !locked("name")) {
      const problem = nameProblem(rows, patch.name ?? "", mode.kind === "update" ? mode.index : undefined);
      if (problem) {
        setError(problem === "empty" ? t_i18n("Give it a name.") : t_i18n("Another entry entered by hand already has this name."));
        return;
      }
    }
    let next: (AssetRow | AssetGroupRow)[];
    let locks = 0;
    if (mode.kind === "add") {
      next = [...rows, isGroup ? newAssetGroupRow(patch) : newAssetRow(patch)];
    } else {
      const indexes = mode.kind === "update" ? [mode.index] : mode.indexes;
      const out = patchRows(section, rows, indexes, patch as Partial<AssetRow | AssetGroupRow>);
      next = out.rows;
      locks = out.locked;
      if (out.changed === 0) {
        onClose();
        return;
      }
    }
    save.mutate(
      { profile: { ...data.profile, [section]: next } as OrganizationProfile, source: "form" },
      {
        onSuccess: () => {
          toast({
            title: mode.kind === "add" ? t_i18n("Entry added") : t_i18n("Changes saved"),
            description: locks
              ? t_i18n("{count, plural, one {# synced row kept the values its source sends} other {# synced rows kept the values their source sends}}", { count: locks })
              : undefined,
            variant: "success",
          });
          onSaved?.();
          onClose();
        },
        onError: (e) => setError(e.message),
      },
    );
  };

  // The fixed vocabularies keep their order; the asset groups are the organization's own (any length): searchable past a handful.
  const select = (key: string, options: Record<string, string>, opts: { none?: boolean; sort?: boolean } = {}) => {
    const pinned: SelectOption[] = [
      ...(bulk ? [{ value: KEEP, label: t_i18n("Keep as is") }] : []),
      ...(opts.none && !bulk ? [{ value: NONE, label: t_i18n("None") }] : []),
    ];
    return (
      <SearchableSelect
        id={`entry-${key}`}
        aria-labelledby={settingsFieldLabelId(`entry-${key}`)}
        options={Object.entries(options).map(([value, label]) => ({ value, label }))}
        pinned={pinned}
        sort={opts.sort ?? false}
        searchable="auto"
        value={draft[key]}
        onValueChange={set(key)}
        disabled={locked(key)}
        className="w-full"
      />
    );
  };
  const translated = (labels: Record<string, string>) => Object.fromEntries(Object.entries(labels).map(([k, v]) => [k, t_i18n(v)]));

  return (
    <DialogContent size="md" data-testid="ctem-entry-dialog">
      <DialogHeader>
        <DialogTitle>{title}</DialogTitle>
        {bulk && (
          <DialogDescription>
            {t_i18n("{count, plural, one {Applies to # row. A synced row keeps the values its source sends.} other {Applies to # rows. A synced row keeps the values its source sends.}}", {
              count: mode.indexes.length,
            })}
          </DialogDescription>
        )}
      </DialogHeader>
      <DialogBody className="space-y-4">
        {!bulk && (
          <SettingsField id="entry-name" label={t_i18n("Name")} required helper={locked("name") ? lockedHelper : undefined}>
            <Input id="entry-name" aria-labelledby={settingsFieldLabelId("entry-name")} value={draft.name} onChange={(e) => set("name")(e.target.value)} disabled={locked("name")} maxLength={200} />
          </SettingsField>
        )}
        {!bulk && !isGroup && (
          <SettingsField id="entry-type" label={t_i18n("Category")} helper={locked("category") ? lockedHelper : undefined}>
            <SearchableSelect
              id="entry-type"
              aria-labelledby={settingsFieldLabelId("entry-type")}
              options={typeOptions(t_i18n)}
              sort={false}
              value={draft.type}
              onValueChange={set("type")}
              disabled={locked("category")}
              className="w-full"
            />
          </SettingsField>
        )}
        {!bulk && isGroup && (
          <SettingsField id="entry-description" label={t_i18n("Description")}>
            <Textarea id="entry-description" aria-labelledby={settingsFieldLabelId("entry-description")} rows={3} value={draft.description} onChange={(e) => set("description")(e.target.value)} disabled={locked("description")} />
          </SettingsField>
        )}
        {!isGroup && (
          <SettingsField id="entry-criticality" label={t_i18n("Criticality")} helper={locked("criticality") ? lockedHelper : undefined}>
            {select("criticality", translated(ASSET_CRITICALITY_LABELS))}
          </SettingsField>
        )}
        <SettingsField id="entry-internet_facing" label={t_i18n("Exposure")} helper={locked("internet_facing") ? lockedHelper : undefined}>
          {select("internet_facing", translated(EXPOSURE_LABELS))}
        </SettingsField>
        <SettingsField id="entry-environment" label={t_i18n("Environment")}>
          {select("environment", translated(ENVIRONMENT_LABELS), { none: true })}
        </SettingsField>
        <SettingsField id="entry-owner" label={t_i18n("Owner")} helper={bulk ? t_i18n("Left empty, each row keeps its owner.") : undefined}>
          <Input id="entry-owner" aria-labelledby={settingsFieldLabelId("entry-owner")} value={draft.owner} onChange={(e) => set("owner")(e.target.value)} maxLength={200} />
        </SettingsField>
        {!isGroup && (
          <SettingsField id="entry-group" label={t_i18n("Asset group")}>
            {select("group", Object.fromEntries(groups.map((g) => [g, g])), { none: true, sort: true })}
          </SettingsField>
        )}
        {error && (
          <p className="whitespace-pre-line content-compact text-feedback-error-primary" role="alert">
            {error}
          </p>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" onClick={onClose}>
          {t_i18n("Cancel")}
        </Button>
        <Button onClick={onSubmit} disabled={save.isPending || !data} data-testid="ctem-entry-save">
          {mode.kind === "add" ? t_i18n("Add") : t_i18n("Save")}
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}
