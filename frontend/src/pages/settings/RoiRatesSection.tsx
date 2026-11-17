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
 * Settings > Organization > Rates by role (#9448, slice O5 of #9206; design
 * dev-docs/roi-outcomes.md 3.4 and 6.3): the hourly rate the time an agent
 * saves is valued at, per role and currency. A role without a rate here uses
 * the organization profile's loaded hourly rate, else the catalog's, and the
 * report labels which one applied. Both modes; no run, no model call.
 */
import { useMemo, useState } from "react";
import { Coins, GitCompare, Pencil, Plus, RotateCcw, Trash2 } from "lucide-react";

import { RoiDefaultDiff } from "@/components/roi/RoiDefaultDiff";
import { originFilter, originOf, useCustomizedLine } from "@/components/roi/roi-origin";
import { RoiOriginBadge } from "@/components/roi/RoiOriginBadge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { OpsRowAction } from "@/components/ui/operations-list";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { focusFirstInvalidField, SettingsCard, SettingsField, SettingsFieldGrid, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { Skeleton } from "@/components/ui/skeleton";
import { useDeleteRoiRate, usePutRoiRate, useRoiRates, type RoiBuiltinRate, type RoiRate } from "@/hooks/api/useRoiCatalogApi";
import { useFormatter } from "@/i18n";
import { CURRENCIES } from "@/lib/organization-profile";
import { amountError, currencyError, parseNumber, rateRoleError } from "./roi-catalog-form";
import { Spinner } from "@filigran/design-system";

/** A source citation on one line, the whole of it as the tooltip, so every row keeps one height (UX round 3 B3). */
function SourceCell({ source }: { source: string }) {
  if (!source) return <>-</>;
  return (
    <div className="max-w-[20rem] truncate" title={source}>
      {source}
    </div>
  );
}

/**
 * The currency filter of a rates table (UX round 1 B2), the table's own pill
 * (data-table gate 3): every currency its rows hold. Its own label key: the
 * shared "Currency" key is a number format in charts (fr "Monétaire").
 */
const currencyFilter = <T extends { currency: string }>(): DataTableFilter<T>[] => [{ key: "currency", label: "Currency of the amounts", values: (r) => [r.currency] }];

function RoiRateDialog({ rate, onOpenChange }: { rate: RoiRate | null; onOpenChange: (open: boolean) => void }) {
  const { t_i18n, intl } = useFormatter();
  // The diff reads like the row: the amount with its currency (UX round 1 B3).
  const money = (amount: number, currency: string) => intl.formatNumber(amount, { style: "currency", currency, maximumFractionDigits: 2 });
  const customizedLine = useCustomizedLine();
  const put = usePutRoiRate();
  // Compare with default (#9836 R3b): a rate over a built-in role shows the catalog's next to it.
  const comparing = rate && rate.default && originOf(rate) === "customized" ? { rate, base: rate.default } : null;
  const [role, setRole] = useState(rate?.role ?? "");
  const [currency, setCurrency] = useState(rate?.currency ?? "EUR");
  const [amount, setAmount] = useState(rate ? String(rate.amount) : "");
  const [source, setSource] = useState(rate?.source ?? "");
  const [submitted, setSubmitted] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const errors = { role: rateRoleError(role), currency: currencyError(currency), amount: amountError(amount) };
  const shown = (error: string | undefined) => (submitted && error ? t_i18n(error) : undefined);
  // The product's currency picker (the organization profile's list); a stored
  // code outside it stays an option.
  const currencyOptions = useMemo(
    () => ((CURRENCIES as readonly string[]).includes(currency) || !currency ? [...CURRENCIES] : [currency, ...CURRENCIES]).map((code) => ({ value: code, label: code })),
    [currency],
  );

  const onSave = () => {
    setSubmitted(true);
    setServerError(null);
    if (errors.role || errors.currency || errors.amount) {
      focusFirstInvalidField([errors.role && "roi-rate-role", errors.currency && "roi-rate-currency", errors.amount && "roi-rate-amount"]);
      return;
    }
    put.mutate(
      { role, currency, amount: parseNumber(amount)!, source: source.trim() },
      { onSuccess: () => onOpenChange(false), onError: (err: Error) => setServerError(err.message) },
    );
  };

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{rate ? t_i18n("Edit the rate of {role}", { role: rate.role }) : t_i18n("Add a rate")}</DialogTitle>
          <DialogDescription>{t_i18n("The cost of one hour of this role's work. The time agents save in units costed at this role is valued at it.")}</DialogDescription>
        </DialogHeader>
        <DialogBody className="-m-1 space-y-4 p-1">
          {comparing && (
            <RoiDefaultDiff
              line={customizedLine(comparing.rate)}
              fields={[
                { label: "Hourly rate", default: money(comparing.base.amount, comparing.rate.currency), current: money(comparing.rate.amount, comparing.rate.currency) },
                { label: "Source", default: comparing.base.source, current: comparing.rate.source },
              ]}
            />
          )}
          <SettingsFieldGrid>
            <SettingsField id="roi-rate-role" label={t_i18n("Role")} required helper={t_i18n("Lower case, such as legal or analyst.")} error={shown(errors.role)}>
              <Input id="roi-rate-role" value={role} disabled={Boolean(rate)} onChange={(e) => setRole(e.target.value)} aria-labelledby={settingsFieldLabelId("roi-rate-role")} aria-describedby={settingsFieldHintId("roi-rate-role")} />
            </SettingsField>
            <SettingsField id="roi-rate-currency" label={t_i18n("Currency")} required helper={t_i18n("A three-letter code, such as EUR or USD.")} error={shown(errors.currency)}>
              <SearchableSelect
                id="roi-rate-currency"
                aria-labelledby={settingsFieldLabelId("roi-rate-currency")}
                aria-describedby={settingsFieldHintId("roi-rate-currency")}
                options={currencyOptions}
                sort={false}
                value={currency}
                onValueChange={setCurrency}
                disabled={Boolean(rate)}
              />
            </SettingsField>
            <SettingsField id="roi-rate-amount" label={t_i18n("Hourly rate")} required helper={t_i18n("The cost of one hour, such as 85.")} error={shown(errors.amount)}>
              <div className="relative">
                <Input
                  id="roi-rate-amount"
                  inputMode="decimal"
                  className="[&_input]:pr-20"
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                  aria-invalid={shown(errors.amount) ? true : undefined}
                  aria-labelledby={settingsFieldLabelId("roi-rate-amount")} aria-describedby={settingsFieldHintId("roi-rate-amount")}
                />
                <span aria-hidden className="pointer-events-none absolute inset-y-0 right-3 flex items-center content-compact text-default-secondary">
                  {t_i18n("{currency} / h", { currency })}
                </span>
              </div>
            </SettingsField>
            <SettingsField id="roi-rate-source" label={t_i18n("Source")} helper={t_i18n("Where the figure comes from, such as the 2026 salary grid.")}>
              <Input id="roi-rate-source" value={source} onChange={(e) => setSource(e.target.value)} aria-labelledby={settingsFieldLabelId("roi-rate-source")} aria-describedby={settingsFieldHintId("roi-rate-source")} />
            </SettingsField>
          </SettingsFieldGrid>
        </DialogBody>
        <DialogFooter>
          {serverError && (
            <p role="alert" className="mr-auto content-compact text-feedback-error-primary">
              {serverError}
            </p>
          )}
          <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={put.isPending}>
            {t_i18n("Cancel")}
          </Button>
          <Button onClick={onSave} disabled={put.isPending} aria-busy={put.isPending || undefined} className="gap-1.5">
            {put.isPending && <Spinner size="sm" tone="inherit" />}
            {t_i18n("Save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function RoiRatesSection() {
  const { t_i18n, intl } = useFormatter();
  const rates = useRoiRates();
  const remove = useDeleteRoiRate();
  const [confirmDialog, confirm] = useConfirm();
  const [editing, setEditing] = useState<{ rate: RoiRate | null } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const money = (amount: number, currency: string) => intl.formatNumber(amount, { style: "currency", currency, maximumFractionDigits: 2 });

  const onRemove = async (r: RoiRate) => {
    // A customized rate goes back to its default; a custom role's rate is removed (#9836 R3b).
    const customized = originOf(r) === "customized";
    const ok = await confirm({
      title: customized
        ? t_i18n("Reset {name} to default?", { name: `${r.role} (${r.currency})` })
        : t_i18n("Remove the rate of {role} in {currency}?", { role: r.role, currency: r.currency }),
      description: customized
        ? t_i18n("The catalog's rate applies again to the ROI figures from now on.")
        : t_i18n("This role then uses the organization's loaded hourly rate, else the catalog's."),
      confirmLabel: customized ? t_i18n("Reset") : t_i18n("Remove"),
      variant: "warning",
    });
    if (!ok) return;
    setActionError(null);
    remove.mutate({ role: r.role, currency: r.currency }, { onError: (err: Error) => setActionError(err.message) });
  };

  const rateColumns: DataTableColumn<RoiRate>[] = [
    { key: "role", header: "Role", cell: (r) => <span className="font-medium text-default-primary">{r.role}</span>, sortValue: (r) => r.role },
    { key: "currency", header: "Currency", hideable: true, cell: (r) => r.currency, sortValue: (r) => r.currency },
    { key: "amount", header: "Hourly rate", hideable: true, cell: (r) => money(r.amount, r.currency), sortValue: (r) => r.amount, align: "right" },
    { key: "source", header: "Source", hideable: true, cell: (r) => <SourceCell source={r.source} />, sortValue: (r) => r.source, hideBelow: "3xl" },
    // Customized over a built-in rate, or Custom for a role the catalog has none for (design 10.4).
    { key: "origin", header: "Origin", hideable: true, cell: (r) => <RoiOriginBadge row={r} />, sortValue: (r) => originOf(r), className: "whitespace-nowrap" },
    {
      key: "actions",
      header: "Actions",
      align: "right",
      cell: (r) =>
        originOf(r) === "customized" ? (
          <div className="flex justify-end gap-1">
            <OpsRowAction icon={Pencil} label={t_i18n("Edit the rate of {role}", { role: r.role })} onClick={() => setEditing({ rate: r })} />
            <OverflowMenu
              className="size-7"
              label={t_i18n("Actions for {name}", { name: r.role })}
              items={[
                { id: `compare-${r.role}-${r.currency}`, label: t_i18n("Compare with default"), icon: GitCompare, onSelect: () => setEditing({ rate: r }) },
                { id: `reset-${r.role}-${r.currency}`, label: t_i18n("Reset to default"), icon: RotateCcw, onSelect: () => void onRemove(r) },
              ]}
            />
          </div>
        ) : (
          <div className="flex justify-end gap-1">
            <OpsRowAction icon={Pencil} label={t_i18n("Edit the rate of {role}", { role: r.role })} onClick={() => setEditing({ rate: r })} />
            <OpsRowAction icon={Trash2} label={t_i18n("Remove the rate of {role}", { role: r.role })} onClick={() => void onRemove(r)} />
          </div>
        ),
    },
  ];

  const builtinColumns: DataTableColumn<RoiBuiltinRate>[] = [
    // The role itself; the catalog's label says how the figure was set, so it
    // is a badge of its own column, never the role (UX round 1 B1).
    { key: "role", header: "Role", cell: (r) => <span className="font-medium text-default-primary">{r.role}</span>, sortValue: (r) => r.role },
    { key: "currency", header: "Currency", hideable: true, cell: (r) => r.currency, sortValue: (r) => r.currency },
    { key: "amount", header: "Hourly rate", hideable: true, cell: (r) => money(r.amount, r.currency), sortValue: (r) => r.amount, align: "right" },
    { key: "basis", header: "Basis", hideable: true, cell: (r) => (r.label ? <Badge variant="outline" className="content-caption whitespace-nowrap">{r.label}</Badge> : "-"), sortValue: (r) => r.label, className: "whitespace-nowrap" },
    { key: "source", header: "Source", hideable: true, cell: (r) => <SourceCell source={r.source} />, hideBelow: "3xl" },
    // Default, or Customized while Your rates sets this role and currency (design 10.4).
    { key: "origin", header: "Origin", hideable: true, cell: (r) => <RoiOriginBadge row={r} />, sortValue: (r) => originOf(r), className: "whitespace-nowrap" },
  ];

  if (rates.isLoading) {
    return (
      <div className="space-y-4" aria-busy="true">
        <Skeleton className="h-40 w-full rounded-sm" />
        <Skeleton className="h-40 w-full rounded-sm" />
      </div>
    );
  }
  if (rates.isError || !rates.data) {
    // The line and a way back without reloading the page (UX round 3 B2).
    return (
      <div className="flex items-center gap-3">
        <p role="alert" className="content-compact text-feedback-error-primary">
          {t_i18n("The rates could not be read.")}
        </p>
        <Button variant="secondary" size="sm" className="gap-1.5" onClick={() => void rates.refetch()}>
          <RotateCcw className="h-3.5 w-3.5" />
          {t_i18n("Retry")}
        </Button>
      </div>
    );
  }

  const own = rates.data.rates;
  return (
    <div className="space-y-6">
      {actionError && (
        <p role="alert" className="content-compact text-feedback-error-primary">
          {actionError}
        </p>
      )}
      <SettingsCard
        icon={Coins}
        title={t_i18n("Your rates")}
        description={t_i18n("A role without a rate here uses the loaded hourly rate of the organization profile, else the catalog rate below.")}
        action={
          own.length > 0 && (
            <Button size="sm" className="gap-1.5" onClick={() => setEditing({ rate: null })}>
              <Plus className="h-4 w-4" />
              {t_i18n("Add a rate")}
            </Button>
          )
        }
      >
        {own.length === 0 ? (
          <EmptyState
            variant="inset"
            icon={Coins}
            title={t_i18n("Every role uses the organization rate.")}
            description={t_i18n("Add a rate when a role, such as legal, costs more or less than the organization's loaded hourly rate.")}
            actionLabel={t_i18n("Add a rate")}
            onAction={() => setEditing({ rate: null })}
          />
        ) : (
          <DataTable
            rows={own}
            columns={rateColumns}
            rowKey={(r) => `${r.role}:${r.currency}`}
            defaultSort={{ key: "role", direction: "asc" }}
            searchFields={(r) => [r.role, r.currency, r.source]}
            searchPlaceholder={t_i18n("Search rates")}
            filters={[...currencyFilter<RoiRate>(), originFilter<RoiRate>(t_i18n)]}
            urlKey="yr_"
            storageKey="settings-roi-own-rates"
            emptyLabel={t_i18n("No results")}
          />
        )}
      </SettingsCard>
      <SettingsCard icon={Coins} title={t_i18n("Catalog rates")} description={t_i18n("Rates the platform falls back to, each with its basis and source. Read-only.")}>
        <DataTable
          rows={rates.data.builtin}
          columns={builtinColumns}
          rowKey={(r) => `${r.role}:${r.currency}`}
          defaultSort={{ key: "role", direction: "asc" }}
          searchFields={(r) => [r.role, r.label, r.currency, r.source]}
          searchPlaceholder={t_i18n("Search rates")}
          filters={[...currencyFilter<RoiBuiltinRate>(), originFilter<RoiBuiltinRate>(t_i18n)]}
          urlKey="cr_"
          storageKey="settings-roi-catalog-rates"
          emptyLabel={t_i18n("No results")}
        />
      </SettingsCard>
      {editing && <RoiRateDialog key={editing.rate ? `${editing.rate.role}:${editing.rate.currency}` : "new"} rate={editing.rate} onOpenChange={(open) => !open && setEditing(null)} />}
      {confirmDialog}
    </div>
  );
}
