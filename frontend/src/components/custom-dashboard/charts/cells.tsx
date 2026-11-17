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
 * A rows cell drawn by its kind (`dashboard_live.CELL_KINDS`): text with
 * ellipsis and its title, a number in the viewer's locale, a date or a
 * date-time in the named formats (`charts/formats.ts`), an entity type, a
 * chip, or chips that end with a `+N` chip when they do not fit their cell.
 *
 * A chip is the design system's `Chip`, the one the modules' own lists draw:
 * a strict hex is its data colour (a wash behind the label), a named colour
 * its severity. Its icon is a Lucide name (`DeckIcon`). Every string is the
 * server's closed data, drawn as text.
 */
import { useRef } from "react";
import { Chip } from "@filigran/design-system";

import { useMeasuredBox } from "@/components/presentation/measured-box";
import { DeckIcon } from "@/components/presentation/DeckIcon";
import { useFormatter } from "@/i18n";
import type { WidgetCell, WidgetCellKind, WidgetChip } from "@/types";

import { chipWidth, fitChips } from "../viz/rows";
import { formatNamedDate } from "./formats";

const HEX = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;
type Severity = "neutral" | "info" | "low" | "medium" | "high" | "critical";
/** A named colour as a chip's severity (the design system's feedback scale). */
const SEVERITY: Readonly<Record<string, Severity>> = {
  success: "low",
  info: "info",
  accent: "info",
  warning: "medium",
  alert: "high",
  danger: "critical",
  neutral: "neutral",
  muted: "neutral",
};

const isChip = (value: unknown): value is WidgetChip =>
  typeof value === "object" && value !== null && !Array.isArray(value) && typeof (value as { label?: unknown }).label === "string";

/** One chip: its label, its colour (a data colour or a severity), its icon. */
function CellChip({ chip, title, fit = false }: { chip: WidgetChip; title?: string; fit?: boolean }) {
  const color = chip.color && HEX.test(chip.color) ? chip.color : undefined;
  const severity = chip.color && !color ? SEVERITY[chip.color] : undefined;
  return (
    <Chip
      label={chip.label}
      color={color}
      severity={severity}
      title={title ?? chip.label}
      // A chip alone in its cell shrinks to it (its label cut); in a row of chips each keeps its size.
      className={fit ? "min-w-0 max-w-full" : "max-w-[160px] shrink-0"}
      data-chip-color={chip.color}
      startIcon={chip.icon ? <DeckIcon name={chip.icon} size={14} /> : undefined}
    />
  );
}

/** Chips in one line: as many as fit, then `+N`, the rest named on hover. */
function ChipsCell({ chips }: { chips: WidgetChip[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const box = useMeasuredBox(ref);
  const shown = box ? fitChips(chips.map((c) => chipWidth(c.label, Boolean(c.icon))), box.width) : chips.length;
  const hidden = chips.slice(shown);
  return (
    <div ref={ref} className="flex min-w-0 flex-nowrap items-center gap-2 overflow-hidden" data-cell="chips">
      {chips.slice(0, shown).map((chip, index) => (
        // The last chip shown, when none is left out, cuts its label rather than the cell cutting it.
        <CellChip key={`${chip.label}-${index}`} chip={chip} fit={index === shown - 1 && hidden.length === 0} />
      ))}
      {hidden.length > 0 && <CellChip chip={{ label: `+${hidden.length}`, color: "accent" }} title={hidden.map((c) => c.label).join(", ")} />}
    </div>
  );
}

/** A cell as its kind draws it; an empty cell is `-`. */
export function Cell({ value, kind }: { value: WidgetCell | undefined; kind: WidgetCellKind | undefined }) {
  const { locale } = useFormatter();
  if (value === null || value === undefined || value === "" || (Array.isArray(value) && value.length === 0)) {
    return <span className="text-default-secondary">-</span>;
  }
  if (kind === "chips" && Array.isArray(value)) return <ChipsCell chips={value.filter(isChip)} />;
  if (isChip(value)) return <span className="flex min-w-0 overflow-hidden"><CellChip chip={value} fit /></span>;
  if (kind === "entityType" && typeof value === "string") {
    return (
      <span className="flex min-w-0 overflow-hidden">
        <CellChip chip={{ label: value.replace(/-/g, " ") }} fit />
      </span>
    );
  }
  let text: string;
  if ((kind === "date" || kind === "datetime") && typeof value === "string") {
    text = formatNamedDate(value, kind === "date" ? "date" : "dateTime", locale);
  } else if (typeof value === "number") {
    text = value.toLocaleString(locale);
  } else if (Array.isArray(value)) {
    text = value.map((chip) => chip.label).join(", ");
  } else {
    text = String(value);
  }
  return (
    <span className="block min-w-0 truncate" title={text}>
      {text}
    </span>
  );
}
