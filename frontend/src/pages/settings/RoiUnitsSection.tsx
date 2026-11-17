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
 * Settings > Organization > Units of work (#9448, slice O5 of #9206; design
 * dev-docs/roi-outcomes.md 6.3 "Catalog and rates"): the custom units an
 * administrator adds (editable, archivable), the built-in units (read-only,
 * their minutes customizable) and the archived ones, collapsed. Both modes.
 * Every built-in row says its Origin, Default or Customized (#9836 R3b,
 * design 10.4), with Compare with default and Reset to default in its menu.
 * Reading and editing the catalog starts no run and calls no model.
 */
import { useMemo, useState } from "react";
import { Archive, GitCompare, Pencil, Plus, RotateCcw, Ruler } from "lucide-react";

import { originFilter, originOf } from "@/components/roi/roi-origin";
import { RoiOriginBadge } from "@/components/roi/RoiOriginBadge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { EmptyState } from "@/components/ui/empty-state";
import { OpsRowAction } from "@/components/ui/operations-list";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { SettingsCard } from "@/components/ui/settings-form";
import { Skeleton } from "@/components/ui/skeleton";
import { useArchiveRoiUnit, useResetRoiBaseline, useRoiRates, useRoiUnits, type RoiUnit } from "@/hooks/api/useRoiCatalogApi";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { rateRoleChoices } from "./roi-catalog-form";
import { RoiUnitDialog } from "./RoiUnitDialog";

type Editing = { unit: RoiUnit | null } | null;

const unitSearch = (u: RoiUnit) => [u.name, u.key, u.role, u.rate_role];

/** The rate role filter of a units table (UX round 1 B2), the table's own pill (data-table gate 3): every rate role its rows hold. */
const RATE_ROLE_FILTER: DataTableFilter<RoiUnit> = { key: "rate_role", label: "Rate role", values: (u) => [u.rate_role] };

export function RoiUnitsSection() {
  const { t_i18n } = useFormatter();
  const isXtmOne = useIsXtmOne();
  const units = useRoiUnits();
  const rates = useRoiRates();
  const archive = useArchiveRoiUnit();
  const reset = useResetRoiBaseline();
  const [confirmDialog, confirm] = useConfirm();
  const [editing, setEditing] = useState<Editing>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const all = useMemo(() => units.data ?? [], [units.data]);
  const custom = all.filter((u) => !u.builtin && !u.archived_at);
  const builtIn = all.filter((u) => u.builtin);
  const archived = all.filter((u) => !u.builtin && u.archived_at);
  const rateRoles = rateRoleChoices(rates.data?.builtin ?? [], rates.data?.rates ?? []);

  // One line per row, the likely figure first (UX round 1 B6).
  const minutes = (u: RoiUnit) => t_i18n("{likely} min ({low} to {high})", { low: u.manual_min_low, likely: u.manual_min_likely, high: u.manual_min_high });
  // The built-in table also filters on the Origin: Default or Customized (design 10.4, round 2 A5).
  const builtInFilters: DataTableFilter<RoiUnit>[] = [RATE_ROLE_FILTER, originFilter<RoiUnit>(t_i18n)];

  const onArchive = async (u: RoiUnit) => {
    const ok = await confirm({
      title: t_i18n("Archive {name}?", { name: u.name }),
      description: t_i18n("Agents can no longer pick it. Its records and the ROI already counted stay, shown as archived."),
      confirmLabel: t_i18n("Archive"),
      variant: "warning",
    });
    if (!ok) return;
    setActionError(null);
    archive.mutate(u.key, { onError: (err: Error) => setActionError(err.message) });
  };

  const onReset = async (u: RoiUnit) => {
    const ok = await confirm({
      title: t_i18n("Reset {name} to default?", { name: u.name }),
      description: t_i18n("The catalog's minutes apply again to the ROI figures from now on."),
      confirmLabel: t_i18n("Reset"),
      variant: "warning",
    });
    if (!ok) return;
    setActionError(null);
    reset.mutate(u.key, { onError: (err: Error) => setActionError(err.message) });
  };

  const nameColumn: DataTableColumn<RoiUnit> = {
    key: "name",
    header: "Name",
    cell: (u) => (
      <div className="min-w-0 max-w-[16rem]">
        <div className="truncate font-medium text-default-primary" title={u.name}>{u.name}</div>
        <div className="truncate content-caption text-default-secondary">{u.key}</div>
      </div>
    ),
    sortValue: (u) => u.name.toLocaleLowerCase(),
  };
  const roleColumn: DataTableColumn<RoiUnit> = { key: "role", header: "Who does it by hand", hideable: true, cell: (u) => u.role || "-", sortValue: (u) => u.role, hideBelow: "3xl" };
  const minutesColumn: DataTableColumn<RoiUnit> = { key: "minutes", header: "Manual minutes", hideable: true, cell: minutes, sortValue: (u) => u.manual_min_likely, align: "right", className: "whitespace-nowrap" };
  const reviewColumn: DataTableColumn<RoiUnit> = { key: "review", header: "Review minutes", hideable: true, cell: (u) => u.review_min, sortValue: (u) => u.review_min, align: "right" };
  const rateColumn: DataTableColumn<RoiUnit> = { key: "rate_role", header: "Rate role", hideable: true, cell: (u) => u.rate_role, sortValue: (u) => u.rate_role, hideBelow: "4xl" };

  const customColumns: DataTableColumn<RoiUnit>[] = [
    // Role, minutes, review and rate are hideable (their shared definitions above), so every
    // table of this section draws Columns (standard v2).
    nameColumn,
    roleColumn,
    minutesColumn,
    reviewColumn,
    rateColumn,
    {
      key: "actions",
      header: "Actions",
      align: "right",
      // Icon actions named by their tooltip, so the row fits a 1024 card in
      // every language (UX round 3 B1).
      cell: (u) => (
        <div className="flex justify-end gap-1">
          <OpsRowAction icon={Pencil} label={t_i18n("Edit {name}", { name: u.name })} onClick={() => setEditing({ unit: u })} />
          <OpsRowAction icon={Archive} label={t_i18n("Archive {name}", { name: u.name })} onClick={() => void onArchive(u)} />
        </div>
      ),
    },
  ];

  // Review minutes leave the built-in table below 3xl so the Origin and Actions columns fit a
  // 1024 card without a sideways scroll (UX round 1 B4); the edit dialog still shows them.
  const builtInColumns: DataTableColumn<RoiUnit>[] = [
    nameColumn,
    roleColumn,
    minutesColumn,
    { ...reviewColumn, hideBelow: "3xl" },
    rateColumn,
    // Its own column, never stacked with the name (design 10.4, round 1 B4).
    { key: "origin", header: "Origin", hideable: true, cell: (u) => <RoiOriginBadge row={u} />, sortValue: (u) => originOf(u), className: "whitespace-nowrap" },
    {
      key: "actions",
      header: "Actions",
      align: "right",
      cell: (u) => {
        const atDefault = originOf(u) === "default";
        return (
          <div className="flex justify-end gap-1">
            <OpsRowAction icon={Pencil} label={t_i18n("Customize {name}", { name: u.name })} onClick={() => setEditing({ unit: u })} />
            <OverflowMenu
              className="size-7"
              label={t_i18n("Actions for {name}", { name: u.name })}
              items={[
                {
                  id: `compare-${u.key}`,
                  label: t_i18n("Compare with default"),
                  icon: GitCompare,
                  onSelect: () => setEditing({ unit: u }),
                  disabled: atDefault,
                  disabledReason: atDefault ? t_i18n("Already the default") : undefined,
                },
                {
                  id: `reset-${u.key}`,
                  label: t_i18n("Reset to default"),
                  icon: RotateCcw,
                  onSelect: () => void onReset(u),
                  disabled: atDefault,
                  disabledReason: atDefault ? t_i18n("Already the default") : undefined,
                },
              ]}
            />
          </div>
        );
      },
    },
  ];

  const archivedColumns: DataTableColumn<RoiUnit>[] = [nameColumn, minutesColumn, reviewColumn, rateColumn];

  if (units.isLoading) {
    return (
      <div className="space-y-4" aria-busy="true">
        <Skeleton className="h-40 w-full rounded-sm" />
        <Skeleton className="h-64 w-full rounded-sm" />
      </div>
    );
  }
  if (units.isError) {
    // The line and a way back without reloading the page (UX round 3 B2).
    return (
      <div className="flex items-center gap-3">
        <p role="alert" className="content-compact text-feedback-error-primary">
          {t_i18n("The units of work could not be read.")}
        </p>
        <Button variant="secondary" size="sm" className="gap-1.5" onClick={() => void units.refetch()}>
          <RotateCcw className="h-3.5 w-3.5" />
          {t_i18n("Retry")}
        </Button>
      </div>
    );
  }

  const customCard = (
    <SettingsCard
      key="custom"
      icon={Ruler}
      title={t_i18n("Custom units")}
      description={t_i18n("The units your organization defined. Agents and fleets pick one to count the work they do.")}
      action={
        <Button size="sm" className="gap-1.5" onClick={() => setEditing({ unit: null })}>
          <Plus className="h-4 w-4" />
          {t_i18n("New unit of work")}
        </Button>
      }
    >
      {custom.length === 0 ? (
        <EmptyState
          variant="inset"
          icon={Ruler}
          title={t_i18n("No custom unit yet")}
          description={t_i18n("Add the work your agents do, such as a contract reviewed or a ticket resolved, with the minutes it takes by hand.")}
          actionLabel={t_i18n("New unit of work")}
          onAction={() => setEditing({ unit: null })}
        />
      ) : (
        <DataTable
          rows={custom}
          columns={customColumns}
          rowKey={(u) => u.key}
          defaultSort={{ key: "name", direction: "asc" }}
          searchFields={unitSearch}
          searchPlaceholder={t_i18n("Search units")}
          filters={[RATE_ROLE_FILTER]}
          urlKey="cu_"
          storageKey="settings-roi-custom-units"
          emptyLabel={t_i18n("No results")}
        />
      )}
    </SettingsCard>
  );

  const builtInCard = (
    <SettingsCard
      key="builtin"
      icon={Ruler}
      title={isXtmOne ? t_i18n("Security units") : t_i18n("Built-in units")}
      description={t_i18n("Built into the platform with estimated minutes. Customize the minutes your organization measured; Reset to default brings the catalog's back.")}
    >
      <DataTable
        rows={builtIn}
        columns={builtInColumns}
        rowKey={(u) => u.key}
        defaultSort={{ key: "name", direction: "asc" }}
        searchFields={unitSearch}
        searchPlaceholder={t_i18n("Search units")}
        filters={builtInFilters}
        urlKey="su_"
        storageKey="settings-roi-security-units"
        emptyLabel={t_i18n("No results")}
      />
    </SettingsCard>
  );

  // The mode's own units first: the built-in security catalog in XTM One mode,
  // the organization's custom units in Copilot mode, where the built-in card
  // holds the corporate catalog (same order as the unit picker, 6.1).
  const cards = isXtmOne ? [builtInCard, customCard] : [customCard, builtInCard];

  return (
    <div className="space-y-6">
      {actionError && (
        <p role="alert" className="content-compact text-feedback-error-primary">
          {actionError}
        </p>
      )}
      {cards}
      {archived.length > 0 && (
        <details className="rounded-sm border border-elevation-subtle-soft">
          <DisclosureSummary size="block" className="px-4 py-3 content-base text-default-primary">
            {t_i18n("Archived units ({count})", { count: archived.length })}
          </DisclosureSummary>
          <div className="px-4 pb-4">
            <p className="mb-3 content-compact text-default-secondary">{t_i18n("Agents can no longer pick these units. Their records stay in the ROI history.")}</p>
            <DataTable
              rows={archived}
              columns={archivedColumns}
              rowKey={(u) => u.key}
              defaultSort={{ key: "name", direction: "asc" }}
              searchFields={unitSearch}
              searchPlaceholder={t_i18n("Search units")}
              filters={[RATE_ROLE_FILTER]}
              urlKey="au_"
              storageKey="settings-roi-archived-units"
              emptyLabel={t_i18n("No results")}
            />
          </div>
        </details>
      )}
      {editing && (
        <RoiUnitDialog
          key={editing.unit?.key ?? "new"}
          open
          onOpenChange={(open) => !open && setEditing(null)}
          unit={editing.unit}
          rateRoles={rateRoles}
        />
      )}
      {confirmDialog}
    </div>
  );
}
