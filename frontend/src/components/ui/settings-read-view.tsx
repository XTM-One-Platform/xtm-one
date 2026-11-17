// SPDX-License-Identifier: Apache-2.0

/**
 * The read view of a settings card (#10855): what a setting holds at rest,
 * drawn as the agent page's Configuration > General card draws it. A tile per
 * value (a small grey label, the value under it), never a disabled input,
 * select or textarea: Edit turns the card into its form, Save or Cancel
 * brings the tiles back.
 *
 *   SettingsReadGrid   the tiles, one column when narrow, two or more when wide.
 *   SettingsReadField  one tile: the label with its info bubble (the field's
 *                      help stays readable at rest), then the value; an empty
 *                      value reads "Not set" in muted text.
 *   SettingsNotSet     the muted "Not set" of an empty value, for a value drawn
 *                      outside a tile (a table cell); with `label`, the muted
 *                      wording of another empty state ("Product default"), so
 *                      an empty value never reads like a set one.
 *
 * The helper line under a field is an edit-mode aid and is not repeated here.
 */
import * as React from "react";

import { InfoTip } from "@/components/ui/info-tip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

interface SettingsReadGridProps extends React.HTMLAttributes<HTMLDivElement> {
  /** Columns on a wide screen (2 by default, as SettingsFieldGrid). */
  columns?: 2 | 3 | 4;
}

const GRID_COLUMNS = { 2: "md:grid-cols-2", 3: "md:grid-cols-2 lg:grid-cols-3", 4: "md:grid-cols-2 lg:grid-cols-4" } as const;

export function SettingsReadGrid({ columns = 2, className, children, ...rest }: SettingsReadGridProps) {
  return (
    <div data-slot="settings-read-grid" className={cn("grid gap-2", GRID_COLUMNS[columns], className)} {...rest}>
      {children}
    </div>
  );
}

/** The muted "Not set" of an empty value, or the muted `label` an empty value falls back to. */
export function SettingsNotSet({ label, className }: { label?: React.ReactNode; className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <span data-slot="settings-read-empty" className={cn("text-default-secondary", className)}>
      {label ?? t_i18n("Not set")}
    </span>
  );
}

/** Whether a value counts as empty for the read view. */
const isEmptyReadValue = (value: React.ReactNode) =>
  value === null || value === undefined || value === false || (typeof value === "string" && value.trim() === "") || (Array.isArray(value) && value.length === 0);

interface SettingsReadFieldProps {
  label: React.ReactNode;
  /** The value; empty (null, undefined, "", []) reads "Not set". */
  value?: React.ReactNode;
  /** The field's info bubble, the same text as in edit mode. */
  info?: React.ReactNode;
  /** A monospace value (an id, a number, a code). */
  mono?: boolean;
  /** Spans the whole row of the grid. */
  wide?: boolean;
  className?: string;
  "data-testid"?: string;
}

export function SettingsReadField({ label, value, info, mono, wide, className, "data-testid": testId }: SettingsReadFieldProps) {
  const { t_i18n } = useFormatter();
  const empty = isEmptyReadValue(value);
  return (
    <div
      data-slot="settings-read-field"
      data-testid={testId}
      className={cn("min-w-0 rounded-sm bg-elevation-hover px-3 py-2.5", wide && "md:col-span-full", className)}
    >
      <div className="flex items-center gap-1.5">
        <p className="content-caption leading-none text-default-secondary">{label}</p>
        {info && <InfoTip content={<div className="max-w-xs space-y-1 content-compact">{info}</div>} label={t_i18n("About this field")} />}
      </div>
      <div className={cn("mt-1.5 min-w-0 break-words content-base", mono && "font-mono-plex")}>{empty ? <SettingsNotSet /> : value}</div>
    </div>
  );
}
