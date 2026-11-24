// SPDX-License-Identifier: Apache-2.0

/**
 * Settings > Organization > Identity and risk: "Reference exchange rates" (#1773).
 * The rates an agent converts an amount priced in another currency with (an OpenCRQ
 * figure in USD against a threshold in EUR). Read view at rest; Edit (administrators)
 * opens the rows; Save sends `fx_rates` alone through PATCH (#1738), never the asset rows.
 * The provenance an agent's accepted proposal left on the field is shown with it.
 */

import { useId, useState } from "react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { DateFilterInput } from "@/components/ui/date-filter-input";
import { Input } from "@/components/ui/input";
import { RowRemoveButton } from "@/components/ui/row-remove-button";
import { SECTION_BAR_ANCHOR_CLASS } from "@/components/ui/section-index";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsCard, SettingsField, SettingsSaveBar, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { useToast } from "@/components/ui/toast";
import { useOrganizationContext, usePatchOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { CURRENCIES } from "@/lib/organization-profile";
import {
  emptyRateRow,
  FX_RATE_PROBLEM_LABELS,
  type FxRateRow,
  missingRateInto,
  rateProblem,
  rateRowOf,
  ratesOf,
  REFERENCE_BOUNDS,
} from "@/lib/organization-reference";
import { OrganizationProvenanceLine } from "./OrganizationProvenanceLine";

/** The currency picker's options (the list the Currency field offers), a stored code outside it kept first. */
const currencyOptionsOf = (value: string) =>
  (value && !(CURRENCIES as readonly string[]).includes(value) ? [value, ...CURRENCIES] : [...CURRENCIES]).map((code) => ({ value: code, label: code }));

const RatesGlyph = ({ className }: { className?: string }) => <Icon name="chart-column" size={16} className={className} aria-hidden />;

export function OrganizationFxRatesCard({ id: anchorId }: { id: string }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const id = useId();
  const { isAdmin } = useAuth();
  const { data } = useOrganizationContext();
  const save = usePatchOrganizationContext();
  const [rows, setRows] = useState<FxRateRow[] | null>(null);

  const profile = data?.profile;
  const rates = profile?.fx_rates ?? [];
  const currency = profile?.currency ?? "";
  const next = rows ? ratesOf(rows) : null;
  const dirty = rows !== null && JSON.stringify(rows) !== JSON.stringify(rates.map(rateRowOf));
  const blocked = rows !== null && next === null;

  const setRow = (index: number, patch: Partial<FxRateRow>) =>
    rows && setRows(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));

  const onSave = () => {
    if (!next) return;
    // #1738: fx_rates alone travels, never the asset rows a sync keeps.
    save.mutate(
      { fields: { fx_rates: next }, based_on_version: data?.version ?? null },
      {
        onSuccess: () => {
          setRows(null);
          toast({ title: t_i18n("Organization profile saved"), variant: "success" });
        },
        onError: (err) => toast({ title: t_i18n("The organization profile was not saved"), description: err.message, variant: "error" }),
      },
    );
  };

  return (
    <SettingsCard
      id={anchorId}
      className={SECTION_BAR_ANCHOR_CLASS}
      data-anchor="card"
      tabIndex={-1}
      data-testid="settings.organization.fx-rates"
      icon={RatesGlyph}
      title={t_i18n("Reference exchange rates")}
      description={t_i18n("The rates agents use to convert an amount priced in another currency into the organization's currency. Without one, the comparison is reported as unknown, never guessed.")}
      action={
        !rows && isAdmin ? (
          <Button
            variant="secondary"
            className="gap-1.5"
            disabled={!profile}
            aria-label={t_i18n("Edit {name}", { name: t_i18n("Reference exchange rates") })}
            onClick={() => setRows(rates.map(rateRowOf))}
            data-testid="settings.organization.edit-fx-rates"
          >
            <Icon name="pencil" size={16} aria-hidden /> {t_i18n("Edit")}
          </Button>
        ) : undefined
      }
    >
      {missingRateInto(currency, rates) && (
        <p className="content-compact text-default-secondary" data-testid="settings.organization.fx-rates.missing">
          {t_i18n("No rate converts into {currency}: an amount in another currency cannot be compared with the thresholds.", { currency })}
        </p>
      )}
      {!rows ? (
        rates.length === 0 ? (
          <p className="text-default-secondary">{t_i18n("No reference rate")}</p>
        ) : (
          <ul className="content-compact" data-testid="settings.organization.fx-rates.list">
            {rates.map((rate) => (
              <li
                key={`${rate.from_currency}-${rate.to_currency}-${rate.as_of}`}
                className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 border-t border-elevation-subtle-soft py-1.5 first:border-t-0"
              >
                <span className="font-mono">{`${rate.from_currency} → ${rate.to_currency}`}</span>
                <span className="font-mono">{rate.rate}</span>
                <span className="text-default-secondary">{t_i18n("As of")} <span className="font-mono">{rate.as_of}</span></span>
                <span className="min-w-0 break-words text-default-secondary">{rate.source}</span>
              </li>
            ))}
          </ul>
        )
      ) : (
        <div className="space-y-3">
          {rows.map((row, index) => {
            const problem = rateProblem(row);
            const rowId = `${id}-rate-${index}`;
            const currencyInvalid = problem === "currency" || problem === "same_currency" ? true : undefined;
            // The date picker draws its own error line; the row line then keeps the help.
            const rowProblem = problem === "date" ? null : problem;
            return (
              <div
                key={index}
                role="group"
                className="space-y-2 border-t border-elevation-subtle-soft pt-3 first:border-t-0 first:pt-0"
                aria-labelledby={`${rowId}-title`}
                aria-describedby={settingsFieldHintId(rowId)}
                data-testid="settings.organization.fx-rates.row"
              >
                <div className="flex items-center justify-between gap-2">
                  <p id={`${rowId}-title`} className="font-medium">{t_i18n("Rate {index}", { index: index + 1 })}</p>
                  <RowRemoveButton
                    label={t_i18n("Remove rate {index}", { index: index + 1 })}
                    onClick={() => setRows(rows.filter((_, i) => i !== index))}
                  />
                </div>
                {/* UX round 1 B2-B4: currency pickers, a visible label on every field, one baseline (no counter).
                    UX round 2 B1: the date picker never gets less than a third of the row, so the date is never cut. */}
                <div className="grid items-start gap-3 sm:grid-cols-2 md:grid-cols-6 xl:grid-cols-12">
                  <SettingsField id={`${rowId}-from`} label={t_i18n("From currency")} className="md:col-span-2">
                    <SearchableSelect
                      id={`${rowId}-from`}
                      aria-labelledby={settingsFieldLabelId(`${rowId}-from`)}
                      aria-invalid={currencyInvalid}
                      options={currencyOptionsOf(row.from_currency)}
                      sort={false}
                      value={row.from_currency || null}
                      onValueChange={(value) => setRow(index, { from_currency: value })}
                    />
                  </SettingsField>
                  <SettingsField id={`${rowId}-to`} label={t_i18n("To currency")} className="md:col-span-2">
                    <SearchableSelect
                      id={`${rowId}-to`}
                      aria-labelledby={settingsFieldLabelId(`${rowId}-to`)}
                      aria-invalid={currencyInvalid}
                      options={currencyOptionsOf(row.to_currency)}
                      sort={false}
                      value={row.to_currency || null}
                      onValueChange={(value) => setRow(index, { to_currency: value })}
                    />
                  </SettingsField>
                  <SettingsField id={`${rowId}-rate`} label={t_i18n("Rate")} className="md:col-span-2">
                    <Input
                      id={`${rowId}-rate`}
                      aria-labelledby={settingsFieldLabelId(`${rowId}-rate`)}
                      inputMode="decimal"
                      aria-invalid={problem === "rate" ? true : undefined}
                      placeholder="0.92"
                      value={row.rate}
                      onChange={(e) => setRow(index, { rate: e.target.value })}
                    />
                  </SettingsField>
                  <SettingsField id={`${rowId}-as-of`} label={t_i18n("As of")} className="md:col-span-2 xl:col-span-3">
                    <DateFilterInput
                      id={`${rowId}-as-of`}
                      className="w-full"
                      aria-labelledby={settingsFieldLabelId(`${rowId}-as-of`)}
                      value={row.as_of}
                      error={problem === "date" ? t_i18n(FX_RATE_PROBLEM_LABELS.date) : undefined}
                      onChange={(day) => setRow(index, { as_of: day })}
                    />
                  </SettingsField>
                  <SettingsField id={`${rowId}-source`} label={t_i18n("Source")} className="md:col-span-4 xl:col-span-3">
                    <Input
                      id={`${rowId}-source`}
                      aria-labelledby={settingsFieldLabelId(`${rowId}-source`)}
                      aria-invalid={problem === "source" ? true : undefined}
                      placeholder={t_i18n("European Central Bank")}
                      title={row.source || undefined}
                      value={row.source}
                      onChange={(e) => setRow(index, { source: e.target.value })}
                    />
                  </SettingsField>
                </div>
                <p
                  id={settingsFieldHintId(rowId)}
                  role={rowProblem ? "alert" : undefined}
                  className={rowProblem ? "content-compact text-feedback-error-primary" : "content-compact text-default-secondary"}
                >
                  {rowProblem
                    ? t_i18n(FX_RATE_PROBLEM_LABELS[rowProblem])
                    : t_i18n("One unit of the first currency is the rate in the second, on that day, from that source.")}
                </p>
              </div>
            );
          })}
          <Button
            variant="secondary"
            className="gap-1.5"
            disabled={rows.length >= REFERENCE_BOUNDS.rates}
            onClick={() => setRows([...rows, emptyRateRow(currency)])}
            data-testid="settings.organization.fx-rates.add"
          >
            <Icon name="plus" size={16} aria-hidden /> {t_i18n("Add a rate")}
          </Button>
          <SettingsSaveBar dirty={dirty} saving={save.isPending} saveDisabled={!dirty || blocked} onSave={onSave} onCancel={() => setRows(null)} />
        </div>
      )}
      <OrganizationProvenanceLine sources={data?.sources} field="fx_rates" />
    </SettingsCard>
  );
}
