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
 * The four run-budget limit fields (cost, tokens, tool calls, duration), the
 * agent Budget tab's grid, shared with the fleet settings panel's "Run
 * defaults". Every empty field shows what it inherits; a limit a
 * non-admin may not set is outlined and explained inline.
 *
 * The fleet panel adds its iteration cap through `iterations`, drawn as one
 * more row of the same grid: same label, icon, "Inherit" reset and
 * "Inherits {value}" placeholder as the four limits.
 *
 * Field standard (frontend-design-system.md, Forms): the label at body size,
 * the help line and the refusal at 12 px (content-compact), never content-caption.
 */
import { useId, useRef, type ReactNode } from "react";
import { Ban, Repeat } from "lucide-react";

import { InfoTip } from "@/components/ui/info-tip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useFormatter } from "@/i18n";
import { MAX_ITERATIONS_CEILING } from "@/lib/constants";
import { BUDGET_LIMITS, type LimitsDraft } from "@/lib/run-budget-limits";
import type { BudgetLimitKey, BudgetPolicy } from "@/types";

/** An iteration cap drawn as a fifth row of the limit grid. */
interface IterationCapRow {
  value: string;
  onChange: (value: string) => void;
  /** What an empty field inherits (the platform's max iterations). */
  inherited: number;
  /** The help line under the field, already translated. */
  help: string;
  /** The info bubble beside the label, already translated. */
  info: string;
}

/**
 * One row of the grid: label with icon and info bubble, "Inherit" reset, field,
 * help or refusal. The label is a real label of the field (`fieldId`), and the
 * caller ties the footer (help or refusal) to the field by aria-describedby.
 * The reset unmounts once the value is cleared, so it hands focus to the row's
 * field instead of letting it fall to the body.
 */
function LimitRow({
  icon: Icon,
  fieldId,
  label,
  info,
  value,
  onReset,
  disabled,
  children,
  footer,
}: {
  icon: React.ElementType;
  fieldId: string;
  label: string;
  info: string;
  value: string;
  onReset: () => void;
  disabled: boolean;
  children: ReactNode;
  footer: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  const rowRef = useRef<HTMLDivElement>(null);
  const reset = () => {
    onReset();
    rowRef.current?.querySelector<HTMLInputElement>("input")?.focus();
  };
  return (
    <div ref={rowRef} className="space-y-1.5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <Label id={`${fieldId}-label`} htmlFor={fieldId} className="flex items-center gap-1.5 text-default-primary">
            <Icon className="h-3.5 w-3.5" aria-hidden /> {label}
          </Label>
          <InfoTip content={<div className="max-w-xs content-compact">{info}</div>} label={t_i18n("About this field")} />
        </div>
        {value !== "" && !disabled && (
          <button
            type="button"
            className="rounded-sm content-compact text-accent hover:text-accent/80 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
            onClick={reset}
          >
            {t_i18n("Inherit")}
          </button>
        )}
      </div>
      {children}
      {footer}
    </div>
  );
}

export function BudgetLimitFields({
  draft,
  onChange,
  policy,
  blocked,
  disabled,
  inheritedLabel,
  columns = 2,
  iterations,
}: {
  draft: LimitsDraft;
  onChange: (key: BudgetLimitKey, value: string) => void;
  policy: BudgetPolicy | undefined;
  blocked: ReadonlySet<BudgetLimitKey>;
  disabled: boolean;
  /** The placeholder of an empty field with a ceiling; defaults to "Inherits {value}". */
  inheritedLabel?: (formatted: string) => string;
  /** 2 on a page, 1 in a narrow side panel. */
  columns?: 1 | 2;
  /** An iteration cap row after the four limits (the fleet "Run defaults"). */
  iterations?: IterationCapRow;
}) {
  const { t_i18n } = useFormatter();
  // Each field is named by its label, and described by its help line or, when blocked, its refusal.
  const idBase = useId();
  const iterationsId = `${idBase}-max_iterations`;
  return (
    <div className={columns === 2 ? "grid gap-4 sm:grid-cols-2" : "grid gap-4"}>
      {BUDGET_LIMITS.map(({ key, label, icon, help, info, step, format }) => {
        const ceiling = policy?.ceilings[key];
        const isBlocked = blocked.has(key);
        const fieldId = `${idBase}-${key}`;
        const errorId = `${fieldId}-error`;
        const helpId = `${fieldId}-help`;
        return (
          <LimitRow
            key={key}
            icon={icon}
            fieldId={fieldId}
            label={t_i18n(label)}
            info={t_i18n(info)}
            value={draft[key]}
            onReset={() => onChange(key, "")}
            disabled={disabled}
            footer={
              isBlocked ? (
                <p id={errorId} className="flex items-start gap-1.5 content-compact text-feedback-error-primary">
                  <Ban className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
                  {t_i18n("Above the platform budget of {value}. Only an administrator can set this.", { value: format(ceiling as number) })}
                </p>
              ) : (
                <p id={helpId} className="content-compact text-default-secondary">{t_i18n(help)}</p>
              )
            }
          >
            <Input
              id={fieldId}
              aria-labelledby={`${fieldId}-label`}
              type="number"
              min="0"
              step={step ?? "1"}
              inputMode="decimal"
              disabled={disabled}
              value={draft[key]}
              onChange={(e) => onChange(key, e.target.value)}
              placeholder={
                ceiling != null
                  ? inheritedLabel
                    ? inheritedLabel(format(ceiling))
                    : t_i18n("Inherits {value}", { value: format(ceiling) })
                  : t_i18n("No limit")
              }
              aria-invalid={isBlocked || undefined}
              aria-describedby={isBlocked ? errorId : helpId}
            />
          </LimitRow>
        );
      })}
      {iterations && (
        <LimitRow
          icon={Repeat}
          fieldId={iterationsId}
          label={t_i18n("Max iterations")}
          info={iterations.info}
          value={iterations.value}
          onReset={() => iterations.onChange("")}
          disabled={disabled}
          footer={
            <p id={`${iterationsId}-help`} className="content-compact text-default-secondary">
              {iterations.help}
            </p>
          }
        >
          <Input
            id={iterationsId}
            aria-labelledby={`${iterationsId}-label`}
            aria-describedby={`${iterationsId}-help`}
            type="number"
            min="1"
            max={MAX_ITERATIONS_CEILING}
            step="1"
            inputMode="numeric"
            disabled={disabled}
            value={iterations.value}
            onChange={(e) => iterations.onChange(e.target.value)}
            placeholder={
              inheritedLabel
                ? inheritedLabel(iterations.inherited.toLocaleString())
                : t_i18n("Inherits {value}", { value: iterations.inherited.toLocaleString() })
            }
          />
        </LimitRow>
      )}
    </div>
  );
}
