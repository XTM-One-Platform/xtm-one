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
 * A table of rows: paging, header modes, row numbers, totals, and per column a
 * width, an alignment, an overflow rule, a cell type (text, a list of chips, a
 * sparkline), formatted values and dynamic text and background colours; the
 * rows themselves may be coloured too (`tableFormat`). Every cell is text and
 * drawn as text; the only link is the row's platform link, checked again.
 */
import { useState, type CSSProperties } from "react";

import { Pagination } from "@/components/ui/pagination";
import { TotalChip } from "@/components/ui/total-chip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { WidgetRowsData } from "@/types";

import { sparklinePoints } from "../spec";
import { cellItems, cellNumbers, columnTotals, readTableOptions, tablePage, type ColumnFormat } from "./chart-options";
import { clickable, useChartEvents, type ChartProps } from "./common";
import { cellText, textRow } from "../viz/shapes";
import { formatNumber, frameOf, optionColors, optionValues, type Cell } from "./dynamic-options";
import { dataHref } from "./links";

const OVERFLOW: Record<ColumnFormat["textOverflow"], CSSProperties> = {
  ellipsis: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  "break-word": { overflowWrap: "break-word" },
  anywhere: { overflowWrap: "anywhere" },
};

function CellSparkline({ cell }: { cell: Cell }) {
  const points = sparklinePoints(cellNumbers(cell), 96, 20, 2);
  if (points.length === 0) return <span>{cell ?? ""}</span>;
  return (
    <svg viewBox="0 0 96 20" width={96} height={20} role="img" aria-label={String(cell ?? "")}>
      <polyline points={points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")} fill="none" stroke="var(--color-accent)" strokeWidth={1.5} />
    </svg>
  );
}

function CellContent({ cell, shown, format }: { cell: Cell; shown: Cell; format?: ColumnFormat }) {
  if (format?.cellType === "sparkline") return <CellSparkline cell={cell} />;
  if (format?.cellType === "array") {
    return (
      <span className="flex flex-wrap gap-1">
        {cellItems(shown).map((item, index) => (
          <span key={`${item}-${index}`} className="rounded-sm bg-elevation-hover px-1.5 py-0.5 content-caption text-default-primary">
            {item}
          </span>
        ))}
      </span>
    );
  }
  return <>{shown ?? ""}</>;
}

export default function DataTable({ data, scope, height }: ChartProps) {
  const { onSelect } = useChartEvents();
  const { t_i18n } = useFormatter();
  const [page, setPage] = useState(0);
  const rows = data as WidgetRowsData;
  const options = readTableOptions(scope.options);
  const fields = frameOf("rows", rows);
  const totals = options.showTotals ? columnTotals(fields) : null;
  // Per column: its formatted values and its dynamic colours, one per row of the whole table.
  const columns = rows.columns.map((column, ci) => {
    const format = Object.prototype.hasOwnProperty.call(options.columnFormat, column.key) ? options.columnFormat[column.key] : undefined;
    const shown = (format && optionValues(format.data, scope)) ?? fields[ci]?.values ?? [];
    return {
      ...column,
      format,
      shown,
      colors: format ? optionColors(format.rowColors, scope) : undefined,
      backgrounds: format ? optionColors(format.rowBackgroundColors, scope) : undefined,
    };
  });
  const rowColors = optionColors(options.rowColors, scope);
  const rowBackgrounds = optionColors(options.rowBackgroundColors, scope);
  const indexes = rows.rows.map((_row, index) => index);
  const current = tablePage(indexes, page, options.count);
  const header = options.headerVisibility !== "none" && (
    <thead className={cn(options.headerVisibility === "fixed" && "sticky top-0 z-10 bg-elevation-default")}>
      <tr className="text-left text-default-secondary">
        {options.showRowNumbers && <th className="w-10 px-2 py-1.5 text-right font-medium">#</th>}
        {columns.map((c) => (
          <th
            key={c.key}
            className="px-2 py-1.5 font-medium"
            style={{ width: c.format?.width, textAlign: c.format?.align }}
          >
            {c.label}
          </th>
        ))}
      </tr>
    </thead>
  );
  const more = rows.total !== null && rows.total > rows.rows.length;
  return (
    <div className="flex min-w-0 flex-col gap-1">
      {/* The total once, at the top, when the rows span pages or the query holds more:
          the pager under the rows never repeats it (#11191). */}
      {(current.pages > 1 || more) && (
        <div className="flex justify-end">
          <TotalChip total={rows.total ?? rows.rows.length} shown={rows.rows.length} />
        </div>
      )}
      <div className="overflow-auto" style={options.headerVisibility === "fixed" ? { maxHeight: height } : undefined}>
        <table className={cn("w-full content-compact", columns.some((c) => c.format?.width) && "table-fixed")}>
          {header}
          <tbody>
            {current.rows.map((index) => {
              const row = rows.rows[index];
              return (
                <tr
                  key={index}
                  {...clickable(onSelect, { name: columns[0]?.key, value: cellText(row[columns[0]?.key ?? ""]), row: textRow(row) })}
                  className={cn("border-t border-elevation-subtle", onSelect && "cursor-pointer hover:bg-elevation-hover")}
                  style={{ color: rowColors?.[index], backgroundColor: rowBackgrounds?.[index] }}
                >
                  {options.showRowNumbers && <td className="px-2 py-1.5 text-right tabular-nums text-default-secondary">{index + 1}</td>}
                  {columns.map((c, ci) => {
                    const href = ci === 0 ? dataHref(row._url) : undefined;
                    const raw = row[c.key];
                    const cell: Cell = typeof raw === "number" ? raw : raw === null || raw === undefined ? null : cellText(raw);
                    const content = <CellContent cell={cell} shown={c.shown[index] ?? null} format={c.format} />;
                    return (
                      <td
                        key={c.key}
                        className={cn("px-2 py-1.5", !rowColors?.[index] && !c.colors?.[index] && "text-default-primary")}
                        style={{
                          width: c.format?.width,
                          textAlign: c.format?.align,
                          color: c.colors?.[index],
                          backgroundColor: c.backgrounds?.[index],
                          ...OVERFLOW[c.format?.textOverflow ?? "break-word"],
                        }}
                      >
                        {href ? (
                          <a href={href} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
                            {content}
                          </a>
                        ) : (
                          content
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
          {totals && (
            <tfoot>
              <tr className="border-t-2 border-elevation-subtle font-semibold text-default-primary">
                {options.showRowNumbers && <td />}
                {columns.map((c, ci) => (
                  <td key={c.key} className="px-2 py-1.5 tabular-nums" style={{ textAlign: c.format?.align }}>
                    {totals[ci] !== null ? formatNumber(totals[ci] as number, { precision: Number.isInteger(totals[ci]) ? 0 : 2 }) : ci === 0 ? t_i18n("Total") : ""}
                  </td>
                ))}
              </tr>
            </tfoot>
          )}
        </table>
      </div>
      {current.pages > 1 && <Pagination page={current.page} pageSize={options.count} total={rows.rows.length} onChange={setPage} />}
    </div>
  );
}
