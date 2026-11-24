// SPDX-License-Identifier: Apache-2.0

/**
 * Settings > Organization > Identity and risk: "Reference exchange rates" (#1773).
 * The rates an agent converts an amount priced in another currency with (an OpenCRQ
 * figure in USD against a threshold in EUR). Read view at rest; Edit (administrators)
 * opens the rows; Save sends `fx_rates` alone through PATCH (#1738), never the asset rows.
 * The provenance an agent's accepted proposal left on the field is shown with it.
 */

import { useId, useState } from "react";
import { Coins } from "lucide-react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { RowRemoveButton } from "@/components/ui/row-remove-button";
import { SECTION_BAR_ANCHOR_CLASS } from "@/components/ui/section-index";
import { SettingsCard, SettingsField, SettingsSaveBar, settingsFieldHintId } from "@/components/ui/settings-form";
import { useToast } from "@/components/ui/toast";
import { useOrganizationContext, usePatchOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
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
      icon={Coins}
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
          <table className="w-full content-compact" data-testid="settings.organization.fx-rates.table">
            <thead>
              <tr className="text-left text-default-secondary">
                <th className="py-1 pr-3 font-normal">{t_i18n("Conversion")}</th>
                <th className="py-1 pr-3 font-normal">{t_i18n("Rate")}</th>
                <th className="py-1 pr-3 font-normal">{t_i18n("As of")}</th>
                <th className="py-1 font-normal">{t_i18n("Source")}</th>
              </tr>
            </thead>
            <tbody>
              {rates.map((rate) => (
                <tr key={`${rate.from_currency}-${rate.to_currency}-${rate.as_of}`} className="border-t border-elevation-subtle-soft">
                  <td className="py-1 pr-3 font-mono">{`${rate.from_currency} → ${rate.to_currency}`}</td>
                  <td className="py-1 pr-3 font-mono">{rate.rate}</td>
                  <td className="py-1 pr-3 font-mono">{rate.as_of}</td>
                  <td className="py-1 break-words">{rate.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      ) : (
        <div className="space-y-3">
          {rows.map((row, index) => {
            const problem = rateProblem(row);
            const rowId = `${id}-rate-${index}`;
            return (
              <SettingsField
                key={index}
                id={rowId}
                label={t_i18n("Rate {index}", { index: index + 1 })}
                helper={t_i18n("One unit of the first currency is the rate in the second, on that day, from that source.")}
                error={problem ? t_i18n(FX_RATE_PROBLEM_LABELS[problem]) : undefined}
                wide
              >
                <div className="flex flex-wrap items-center gap-2" data-testid="settings.organization.fx-rates.row">
                  <Input
                    id={rowId}
                    className="w-20"
                    aria-label={t_i18n("From currency")}
                    aria-describedby={settingsFieldHintId(rowId)}
                    aria-invalid={problem === "currency" || problem === "same_currency" ? true : undefined}
                    placeholder="USD"
                    maxLength={3}
                    value={row.from_currency}
                    onChange={(e) => setRow(index, { from_currency: e.target.value.toUpperCase() })}
                  />
                  <span aria-hidden>→</span>
                  <Input
                    className="w-20"
                    aria-label={t_i18n("To currency")}
                    aria-invalid={problem === "currency" || problem === "same_currency" ? true : undefined}
                    placeholder={currency || "EUR"}
                    maxLength={3}
                    value={row.to_currency}
                    onChange={(e) => setRow(index, { to_currency: e.target.value.toUpperCase() })}
                  />
                  <Input
                    className="w-28"
                    inputMode="decimal"
                    aria-label={t_i18n("Rate")}
                    aria-invalid={problem === "rate" ? true : undefined}
                    placeholder="0.92"
                    value={row.rate}
                    onChange={(e) => setRow(index, { rate: e.target.value })}
                  />
                  <Input
                    className="w-36"
                    type="date"
                    aria-label={t_i18n("As of")}
                    aria-invalid={problem === "date" ? true : undefined}
                    value={row.as_of}
                    onChange={(e) => setRow(index, { as_of: e.target.value })}
                  />
                  <Input
                    className="min-w-40 flex-1"
                    aria-label={t_i18n("Source")}
                    aria-invalid={problem === "source" ? true : undefined}
                    placeholder={t_i18n("European Central Bank")}
                    maxLength={REFERENCE_BOUNDS.source}
                    value={row.source}
                    onChange={(e) => setRow(index, { source: e.target.value })}
                  />
                  <RowRemoveButton
                    label={t_i18n("Remove rate {index}", { index: index + 1 })}
                    onClick={() => setRows(rows.filter((_, i) => i !== index))}
                  />
                </div>
              </SettingsField>
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
