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
 * A list of rows in its style (`listStyle`, `viz/rows.ts`): XTM One's compact
 * list, a table (a header, typed cells, an arrow column), rows (a leading
 * icon, typed cells, a hover accent, a trailing arrow) or a grid of cards.
 *
 * The cells are drawn by their kind (`cells.tsx`). A row opens its platform
 * link - built by the server, checked again (`safeExternalUrl`) - in a new
 * tab; with the dashboard's interactions (`onSelect`) a click hands the row
 * to them instead. A row without a link does not open.
 */
import type { CSSProperties, ReactNode } from "react";
import { ChevronRight } from "lucide-react";

import { DeckIcon } from "@/components/presentation/DeckIcon";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { WidgetRowsData } from "@/types";

import { cellText, textRow } from "../viz/shapes";
import {
  ARROW_COLUMN,
  cardOf,
  cardRows,
  columnPercent,
  hasIconColumn,
  ICON_COLUMN,
  readListOptions,
  TABLE_HEADER,
  type ListOptions,
} from "../viz/rows";
import { Cell } from "./cells";
import { clickable, useChartEvents } from "./common";
import { formatNamedDate } from "./formats";
import { resolveColor } from "./colors";
import { dataHref } from "./links";

type Row = WidgetRowsData["rows"][number];

/** What a row does when it is clicked: the interactions', else its link (a new tab), else nothing. */
function RowLink({ row, data, className, style, children, label }: { row: Row; data: WidgetRowsData; className?: string; style?: CSSProperties; children: ReactNode; label?: string }) {
  const { onSelect } = useChartEvents();
  const first = data.columns[0]?.key ?? "";
  if (onSelect) {
    const click = clickable(onSelect, { name: first, value: cellText(row[first]), row: textRow(row) });
    return (
      <div {...click} className={cn(className, click.className)} style={style} data-row="">
        {children}
      </div>
    );
  }
  const href = dataHref(row._url);
  if (!href) {
    return (
      <div className={cn(className, "cursor-default")} style={style} data-row="">
        {children}
      </div>
    );
  }
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className={className} style={style} aria-label={label} data-row="">
      {children}
    </a>
  );
}

function More({ data }: { data: WidgetRowsData }) {
  const { t_i18n } = useFormatter();
  if (data.total === null || data.total <= data.rows.length) return null;
  return (
    <span className="content-caption text-default-secondary">
      {t_i18n("{shown} of {total}", { shown: data.rows.length, total: data.total })}
    </span>
  );
}

const rowName = (data: WidgetRowsData, row: Row): string => cellText(row[data.columns.find((c) => c.key === "name")?.key ?? data.columns[0]?.key ?? ""]);

/** XTM One's list: the first column, linked, the rest after it. */
function CompactList({ data }: { data: WidgetRowsData }) {
  const { onSelect } = useChartEvents();
  const [first, ...rest] = data.columns;
  if (!first) return null;
  return (
    <ul className="flex flex-col">
      {data.rows.map((row, index) => {
        const href = dataHref(row._url);
        const label = cellText(row[first.key]) || "-";
        return (
          <li
            key={index}
            {...clickable(onSelect, { name: first.key, value: cellText(row[first.key]), row: textRow(row) })}
            className={cn("flex items-baseline justify-between gap-3 border-b border-elevation-subtle py-1.5 content-compact last:border-b-0", onSelect && "cursor-pointer")}
          >
            {href ? (
              <a href={href} target="_blank" rel="noopener noreferrer" className="min-w-0 truncate text-accent hover:underline">
                {label}
              </a>
            ) : (
              <span className="min-w-0 truncate text-default-primary">{label}</span>
            )}
            <span className="shrink-0 truncate text-default-secondary">{rest.map((c) => cellText(row[c.key])).filter(Boolean).join(" · ")}</span>
          </li>
        );
      })}
    </ul>
  );
}

/** A table: a 42 px header, rows of equal columns, an arrow column; an icon column when rows carry an icon. */
function TableList({ data, options }: { data: WidgetRowsData; options: ListOptions }) {
  const { t_i18n } = useFormatter();
  const icons = hasIconColumn(data.rows);
  const share = `${100 / Math.max(1, data.columns.length)}%`;
  return (
    <div className="min-w-0" role="table" data-list-style="table">
      <div role="row" className="flex items-center border-b border-elevation-subtle" style={{ height: TABLE_HEADER }}>
        {icons && <span role="columnheader" style={{ width: ICON_COLUMN }} className="shrink-0" aria-label={t_i18n("Type")} />}
        <div className="flex min-w-0 flex-1">
          {data.columns.map((column) => (
            <span key={column.key} role="columnheader" className="truncate px-2 text-xs font-bold text-default-primary" style={{ width: share }}>
              {column.label}
            </span>
          ))}
        </div>
        <span style={{ width: ARROW_COLUMN }} className="shrink-0" />
      </div>
      {data.rows.map((row, index) => (
        <RowLink
          key={index}
          row={row}
          data={data}
          label={rowName(data, row)}
          className="flex items-center border-b border-elevation-subtle text-[13px] text-default-primary hover:bg-elevation-hover"
          style={{ height: options.rowHeight }}
        >
          {icons && (
            <span className="flex shrink-0 justify-center" style={{ width: ICON_COLUMN, color: resolveColor(row._color) ?? "var(--color-accent)" }} data-row-icon={row._icon}>
              <DeckIcon name={row._icon} size={20} />
            </span>
          )}
          <div role="row" className="flex min-w-0 flex-1 items-center">
            {data.columns.map((column) => (
              <span key={column.key} role="cell" className="min-w-0 px-2" style={{ width: share }}>
                <Cell value={row[column.key]} kind={column.kind} />
              </span>
            ))}
          </div>
          <span className="flex shrink-0 justify-center text-default-secondary" style={{ width: ARROW_COLUMN, color: resolveColor(options.arrowColor) }}>
            {row._url && <ChevronRight className="h-4 w-4" aria-hidden="true" data-row-arrow="" />}
          </span>
        </RowLink>
      ))}
    </div>
  );
}

/** Rows: a leading icon, typed cells of equal share, a hover accent, an arrow on the rows that open. */
function RowsList({ data, options }: { data: WidgetRowsData; options: ListOptions }) {
  const share = `${columnPercent(data.columns.length)}%`;
  return (
    <div className="flex min-w-0 flex-col" data-list-style="rows">
      {data.rows.map((row, index) => (
        <RowLink
          key={index}
          row={row}
          data={data}
          label={rowName(data, row)}
          className={cn(
            "flex items-center gap-0 rounded-sm border-b border-elevation-subtle px-4 text-sm text-default-primary transition-[background,box-shadow] duration-150",
            row._url && "hover:bg-elevation-hover hover:shadow-[inset_2px_0_0_0_var(--color-accent)]",
          )}
          style={{ height: options.rowHeight }}
        >
          {row._icon && (
            <span className="flex shrink-0 text-accent" style={{ width: ICON_COLUMN, color: resolveColor(row._color) }} data-row-icon={row._icon}>
              <DeckIcon name={row._icon} size={24} />
            </span>
          )}
          <div className="flex min-w-0 flex-1 flex-nowrap items-center">
            {data.columns.map((column) => (
              <span key={column.key} className="min-h-5 min-w-0 pr-2" style={{ width: share }}>
                <Cell value={row[column.key]} kind={column.kind} />
              </span>
            ))}
          </div>
          <span className="flex w-6 shrink-0 justify-end text-default-secondary" style={{ color: resolveColor(options.arrowColor) }}>
            {row._url && <ChevronRight className="h-5 w-5" aria-hidden="true" data-row-arrow="" />}
          </span>
        </RowLink>
      ))}
    </div>
  );
}

/** Cards: `cardsPerRow` a row, each an avatar of its icon, its name and when it was last updated. */
function CardGrid({ data, options }: { data: WidgetRowsData; options: ListOptions }) {
  const { t_i18n, locale } = useFormatter();
  return (
    <div className="flex flex-col gap-6 pb-2.5" data-list-style="cards">
      {cardRows(data.rows, options.cardsPerRow).map((line, lineIndex) => (
        <div key={lineIndex} className="grid gap-6" style={{ gridTemplateColumns: `repeat(${options.cardsPerRow}, minmax(0, 1fr))` }}>
          {line.map((row, index) => {
            const card = cardOf(data, row);
            return (
              <RowLink
                key={index}
                row={row}
                data={data}
                label={card.title}
                className="flex min-w-0 items-center gap-4 rounded-sm border border-elevation-subtle p-4 hover:bg-elevation-hover"
              >
                <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent text-[var(--bg-elevation-default-layer-0)]" data-card-avatar={card.icon ?? ""}>
                  <DeckIcon name={card.icon} size={22} />
                </span>
                <span className="flex min-w-0 flex-col">
                  <span className="truncate content-compact text-default-primary">{card.title}</span>
                  {card.date && (
                    <span className="content-caption text-default-secondary">
                      {t_i18n("Updated on {date}", { date: formatNamedDate(card.date, "date", locale) })}
                    </span>
                  )}
                </span>
              </RowLink>
            );
          })}
        </div>
      ))}
    </div>
  );
}

/** The rows in the style their options ask for, scrolled within `height`. */
export default function RowsView({ data, options, height }: { data: WidgetRowsData; options: Record<string, unknown>; height: number }) {
  const read = readListOptions(options);
  const drawn =
    read.listStyle === "table" ? (
      <TableList data={data} options={read} />
    ) : read.listStyle === "rows" ? (
      <RowsList data={data} options={read} />
    ) : read.listStyle === "cards" ? (
      <CardGrid data={data} options={read} />
    ) : (
      <CompactList data={data} />
    );
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className={cn(read.listStyle !== "compact" && "overflow-auto")} style={read.listStyle !== "compact" ? { maxHeight: height } : undefined}>
        {drawn}
      </div>
      <More data={data} />
    </div>
  );
}
