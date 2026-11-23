// SPDX-License-Identifier: Apache-2.0

/**
 * Add, update and bulk-update dialog of the CTEM Inventory's rows (#1367, point 2, Sam 23 Nov 2026: "more normal
 * management", as in OpenAEV). One dialog for the assets and the asset groups: Add creates a row entered by hand,
 * Update changes one row, the bulk form changes the selected rows. Since #1543 each write is one row API call (the
 * asset record or the group, by id), never the whole profile. A field a platform sync sends is locked on its rows
 * (point 3): it renders disabled with the reason, and the backend refuses the same change.
 */
import { useState } from "react";
import { Textarea } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { SearchableSelect, type SelectOption } from "@/components/ui/searchable-select";
import { SettingsField, settingsFieldLabelId } from "@/components/ui/settings-form";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import {
  useAssetGroups,
  useAssetRecord,
  useCreateAssetGroup,
  useCreateAssetRecord,
  useUpdateAssetGroups,
  useUpdateAssetRecords,
  type RowsOutcome,
} from "@/hooks/api/useOrganizationAssetsApi";
import { useFormatter } from "@/i18n";
import { ASSET_CRITICALITY_LABELS, parseTypeValue, typeOptions, typeValue } from "@/lib/asset-taxonomy";
import { ENVIRONMENT_LABELS, EXPOSURE_LABELS } from "@/lib/organization-import-filters";
import { fieldEditable, nameProblem, newAssetGroupRow, newAssetRow, rowPatches, type InventorySection, type RowRef } from "./inventory-entries";

/** What the dialog does: create a row, change one (by id), or change the selected rows. */
export type EntryDialogMode = { kind: "add" } | { kind: "update"; id: string } | { kind: "bulk"; rows: RowRef[] };

/** The value of a bulk select that leaves the field as each row has it. */
const KEEP = "__keep__";
/** The value of a select for "no value" (a Select item cannot be the empty string). */
const NONE = "__none__";

type Draft = Record<string, string>;
/** The values of one row the form starts from. */
type RowValues = RowRef & Record<string, unknown>;

const text = (value: unknown) => (typeof value === "string" ? value : "");

function draftOf(section: InventorySection, row: RowValues | undefined, bulk: boolean): Draft {
  if (bulk) return { criticality: KEEP, internet_facing: KEEP, environment: KEEP, group: KEEP, owner: "" };
  if (section === "asset_groups") {
    const g = row ?? (newAssetGroupRow({}) as unknown as RowValues);
    return {
      name: text(g.name),
      description: text(g.description),
      environment: text(g.environment) || NONE,
      internet_facing: text(g.internet_facing) || "unknown",
      owner: text(g.owner),
    };
  }
  const a = row ?? (newAssetRow({}) as unknown as RowValues);
  return {
    name: text(a.name),
    type: typeValue(text(a.category), text(a.subcategory)),
    criticality: text(a.criticality) || "unknown",
    environment: text(a.environment) || NONE,
    internet_facing: text(a.internet_facing) || "unknown",
    owner: text(a.owner),
    group: text(a.group) || NONE,
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
  /** Called once the rows are written (the caller clears its selection). */
  onSaved?: () => void;
}

/** The dialog's title per list and mode. @i18n-keys */
const DIALOG_TITLES = {
  assets_exposure: { add: "Add an asset", update: "Update the asset", bulk: "Update the selected assets" },
  asset_groups: { add: "Add an asset group", update: "Update the asset group", bulk: "Update the selected asset groups" },
} as const;

function dialogTitle(section: InventorySection, mode: EntryDialogMode): string {
  return DIALOG_TITLES[section][mode.kind];
}

export function InventoryEntryDialog({ section, mode, onOpenChange, onSaved }: InventoryEntryDialogProps) {
  const open = mode !== null;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {mode && <EntryLoader key={JSON.stringify(mode)} section={section} mode={mode} onClose={() => onOpenChange(false)} onSaved={onSaved} />}
    </Dialog>
  );
}

/** Reads the row an update starts from (one record, or the group from the group list), then mounts the form. */
function EntryLoader({ section, mode, onClose, onSaved }: { section: InventorySection; mode: EntryDialogMode; onClose: () => void; onSaved?: () => void }) {
  const { t_i18n } = useFormatter();
  const isGroup = section === "asset_groups";
  const groups = useAssetGroups();
  const record = useAssetRecord(!isGroup && mode.kind === "update" ? mode.id : undefined);
  let row: RowValues | undefined;
  if (mode.kind === "update") {
    row = isGroup ? (groups.data?.items.find((g) => g.id === mode.id) as RowValues | undefined) : (record.data as RowValues | undefined);
    if (!row) {
      return (
        <DialogContent size="md" data-testid="ctem-entry-dialog">
          <DialogHeader>
            <DialogTitle>{t_i18n(dialogTitle(section, mode))}</DialogTitle>
          </DialogHeader>
          <DialogBody className="space-y-3">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </DialogBody>
        </DialogContent>
      );
    }
  }
  return <EntryForm section={section} mode={mode} row={row} groups={groups.data?.items ?? []} onClose={onClose} onSaved={onSaved} />;
}

function EntryForm({
  section,
  mode,
  row,
  groups,
  onClose,
  onSaved,
}: {
  section: InventorySection;
  mode: EntryDialogMode;
  row: RowValues | undefined;
  groups: { id: string; name: string }[];
  onClose: () => void;
  onSaved?: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const isGroup = section === "asset_groups";
  const createAsset = useCreateAssetRecord();
  const updateAssets = useUpdateAssetRecords();
  const createGroup = useCreateAssetGroup();
  const updateGroups = useUpdateAssetGroups();
  const pending = createAsset.isPending || updateAssets.isPending || createGroup.isPending || updateGroups.isPending;
  const bulk = mode.kind === "bulk";
  const [draft, setDraft] = useState<Draft>(() => draftOf(section, row, bulk));
  const [error, setError] = useState("");
  const set = (key: string) => (value: string) => setDraft((d) => ({ ...d, [key]: value }));
  const groupNames = groups.map((g) => g.name).filter((n) => n.trim());
  // A field the row's source sends is locked on that row (the bulk form skips the rows it cannot change).
  const locked = (key: string) => mode.kind === "update" && Boolean(row) && !fieldEditable(section, row!, key);
  const lockedHelper = t_i18n("Set by the source that syncs this row.");

  const title = t_i18n(dialogTitle(section, mode));

  const done = (locks: number, outcome?: RowsOutcome) => {
    if (outcome && outcome.refused.length > 0) {
      setError(outcome.refused.map((r) => r.reason).join("\n"));
      if (outcome.written === 0) return;
    }
    toast({
      title: mode.kind === "add" ? t_i18n("Entry added") : t_i18n("Changes saved"),
      description: locks
        ? t_i18n("{count, plural, one {# synced row kept the values its source sends} other {# synced rows kept the values their source sends}}", { count: locks })
        : undefined,
      variant: "success",
    });
    onSaved?.();
    if (!outcome || outcome.refused.length === 0) onClose();
  };

  const onSubmit = () => {
    const patch = patchOf(section, draft, bulk);
    if (!bulk && !locked("name")) {
      const problem = nameProblem(isGroup ? groups : [], patch.name ?? "", mode.kind === "update" ? mode.id : undefined);
      if (problem) {
        setError(problem === "empty" ? t_i18n("Give it a name.") : t_i18n("Another entry entered by hand already has this name."));
        return;
      }
    }
    const onError = (e: Error) => setError(e.message);
    if (mode.kind === "add") {
      const value = isGroup ? newAssetGroupRow(patch) : newAssetRow(patch);
      const body = Object.fromEntries(Object.entries(value).filter(([, v]) => typeof v === "string")) as Record<string, string>;
      (isGroup ? createGroup : createAsset).mutate(body, { onSuccess: () => done(0), onError });
      return;
    }
    const rows = mode.kind === "update" ? [row!] : mode.rows;
    const { patches, locked: locks } = rowPatches(section, rows, patch);
    if (patches.length === 0) {
      if (locks) done(locks);
      else onClose();
      return;
    }
    // One write per row; the rows of one bulk form share a patch once the locked fields are left out.
    const groupsOfPatch = new Map<string, { ids: string[]; patch: Record<string, string> }>();
    for (const p of patches) {
      const key = JSON.stringify(p.patch);
      const entry = groupsOfPatch.get(key) ?? { ids: [], patch: p.patch as Record<string, string> };
      entry.ids.push(p.id);
      groupsOfPatch.set(key, entry);
    }
    const update = isGroup ? updateGroups : updateAssets;
    void Promise.all([...groupsOfPatch.values()].map((g) => update.mutateAsync(g)))
      .then((outcomes) =>
        done(locks, {
          written: outcomes.reduce((n, o) => n + o.written, 0),
          refused: outcomes.flatMap((o) => o.refused),
        }),
      )
      .catch(onError);
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
              count: mode.rows.length,
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
            {select("group", Object.fromEntries(groupNames.map((g) => [g, g])), { none: true, sort: true })}
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
        <Button onClick={onSubmit} disabled={pending} data-testid="ctem-entry-save">
          {mode.kind === "add" ? t_i18n("Add") : t_i18n("Save")}
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}
