// SPDX-License-Identifier: Apache-2.0

/**
 * CSV export of a table's rows: what `DataTable`'s Export button writes.
 *
 * RFC 4180 quoting (a field holding a comma, a quote or a line break is quoted,
 * its quotes doubled), CRLF line ends, and a guard against formula injection:
 * a TEXT cell a spreadsheet would read as a formula (`=`, `+`, `-`, `@`, a tab
 * or a carriage return first) is prefixed with a quote, so an agent or a user
 * name typed as `=HYPERLINK(...)` opens as text. Numbers are written as they
 * are, a negative cost included. Pure: the download itself is `saveBlobAs()`.
 */

export type CsvValue = string | number | boolean | null | undefined;

export interface CsvColumn<T> {
  header: string;
  value: (row: T) => CsvValue;
}

const FORMULA_START = /^[=+\-@\t\r]/;

export function csvCell(value: CsvValue): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "";
  if (typeof value === "boolean") return value ? "true" : "false";
  const text = FORMULA_START.test(value) ? `'${value}` : value;
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function toCsv<T>(rows: readonly T[], columns: readonly CsvColumn<T>[]): string {
  const lines = [columns.map((column) => csvCell(column.header)).join(",")];
  for (const row of rows) lines.push(columns.map((column) => csvCell(column.value(row))).join(","));
  return `${lines.join("\r\n")}\r\n`;
}

/** The file a CSV is saved as: `<stem>-<YYYY-MM-DD>.csv`, the stem made file-safe. */
export function csvFilename(stem: string, at: Date = new Date()): string {
  const safe = stem.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "export";
  return `${safe}-${at.toISOString().slice(0, 10)}.csv`;
}

/** A CSV as a file: UTF-8 with a byte order mark, so spreadsheets read accents right. */
export function csvBlob(csv: string): Blob {
  return new Blob(["\ufeff", csv], { type: "text/csv;charset=utf-8" });
}
