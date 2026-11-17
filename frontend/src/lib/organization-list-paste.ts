// SPDX-License-Identifier: Apache-2.0

/**
 * "Paste a list" and CSV import for the lists of the organization profile (#12256, slice 2):
 * the lines a person pastes (one row per line, the fields split by a tab, a comma, a semicolon
 * or " - ", e.g. "Jira, Atlassian") or the CSV the list's Export wrote (its first line names
 * the columns), read into rows with a status per line: new, duplicate (of a row the list holds
 * or of an earlier line) or an error (no name, a value a choice does not know). Pure, so the
 * dialog and its tests share it.
 */

/** The reasons a pasted line is not added. @i18n-keys */
export const PASTE_ERRORS = {
  noName: "No name on this line",
  badChoice: "Unknown value",
} as const;

type PasteError = (typeof PASTE_ERRORS)[keyof typeof PASTE_ERRORS];

export interface PasteSpec<V> {
  /** The fields in the order a line gives them when it has no header. */
  fields: readonly (keyof V & string)[];
  /** The value of a new row before the line's fields are set. */
  empty: V;
  /** The field that names the row (required). */
  name: keyof V & string;
  /**
   * The words a header cell may use for each field (the field key always works), compared
   * trimmed and case-insensitive: the column header as Export writes it, in any locale.
   */
  headers?: Partial<Record<keyof V & string, readonly string[]>>;
  /**
   * The fields that take a choice: each stored value with the words that read as it (its
   * label in any locale, the value itself always works). An empty cell keeps the default.
   */
  choices?: Partial<Record<keyof V & string, Record<string, readonly string[]>>>;
  /** The identity two rows are duplicates by ("" = never a duplicate). */
  identity: (value: V) => string;
}

export interface PasteLine<V> {
  /** 1-based line number in what was pasted. */
  line: number;
  text: string;
  value: V | null;
  status: "new" | "duplicate" | "error";
  error?: PasteError;
  /** The field the error is about (a choice). */
  field?: string;
}

const norm = (text: string) => text.trim().toLowerCase();

/** Splits one CSV line on commas, RFC 4180 quotes honoured ("a, b" stays one field, "" is a quote). */
function splitCsv(line: string): string[] {
  const cells: string[] = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < line.length; i += 1) {
    const char = line[i];
    if (quoted) {
      if (char === '"' && line[i + 1] === '"') {
        cell += '"';
        i += 1;
      } else if (char === '"') quoted = false;
      else cell += char;
    } else if (char === '"' && !cell.trim()) {
      quoted = true;
      cell = "";
    } else if (char === ",") {
      cells.push(cell);
      cell = "";
    } else cell += char;
  }
  cells.push(cell);
  return cells;
}

/** A cell the list's Export protected against formula injection reads back without its quote. */
const unprotect = (cell: string) => (/^'[=+\-@\t\r]/.test(cell) ? cell.slice(1) : cell);

/** The fields of one pasted line: tab first, then a comma (CSV quotes honoured), a semicolon, then " - ". */
export function splitLine(line: string): string[] {
  let cells: string[];
  if (line.includes("\t")) cells = line.split("\t");
  else if (line.includes(",")) cells = splitCsv(line);
  else if (line.includes(";")) cells = line.split(";");
  else if (line.includes(" - ")) cells = line.split(" - ");
  else cells = [line];
  return cells.map((cell) => unprotect(cell.trim()));
}

/** The field each column of a header line maps to, or null when the line is not a header (its first cell names no field). */
function headerMap<V>(cells: readonly string[], spec: PasteSpec<V>): (string | null)[] | null {
  const lookup = new Map<string, string>();
  for (const field of spec.fields) {
    lookup.set(norm(field), field);
    lookup.set(norm(field.replace(/_/g, " ")), field);
    for (const word of spec.headers?.[field] ?? []) lookup.set(norm(word), field);
  }
  const mapped = cells.map((cell) => lookup.get(norm(cell)) ?? null);
  return mapped[0] !== null && mapped.includes(spec.name) ? mapped : null;
}

function choiceOf(words: Record<string, readonly string[]>, cell: string): string | null {
  const wanted = norm(cell);
  for (const [value, labels] of Object.entries(words)) {
    if (norm(value) === wanted || labels.some((label) => norm(label) === wanted)) return value;
  }
  return null;
}

/**
 * Reads *text* into lines with their status. A first line that names the columns (the CSV
 * Export writes) maps the fields by name; otherwise they come in `spec.fields` order. Blank
 * lines are skipped. A line is a duplicate when its identity is already in *existing* or on an
 * earlier line.
 */
export function parsePaste<V>(text: string, spec: PasteSpec<V>, existing: readonly V[]): PasteLine<V>[] {
  const lines = text.replace(/^\ufeff/, "").split(/\r?\n/);
  const seen = new Set(existing.map((value) => norm(spec.identity(value))).filter(Boolean));
  let columns: readonly (string | null)[] = spec.fields;
  const out: PasteLine<V>[] = [];
  lines.forEach((raw, index) => {
    if (!raw.trim()) return;
    const cells = splitLine(raw);
    if (out.length === 0 && columns === spec.fields) {
      const header = headerMap(cells, spec);
      if (header) {
        columns = header;
        return;
      }
    }
    const value = { ...spec.empty } as Record<string, unknown>;
    let error: PasteLine<V>["error"];
    let field: string | undefined;
    columns.forEach((key, at) => {
      const cell = cells[at]?.trim() ?? "";
      if (!key || !cell) return;
      const words = spec.choices?.[key as keyof V & string];
      if (words) {
        const choice = choiceOf(words, cell);
        if (choice === null) {
          if (!error) {
            error = PASTE_ERRORS.badChoice;
            field = key;
          }
          return;
        }
        value[key] = choice;
      } else value[key] = cell;
    });
    const row = value as V;
    const line = index + 1;
    if (!String(value[spec.name] ?? "").trim()) {
      out.push({ line, text: raw, value: null, status: "error", error: PASTE_ERRORS.noName });
      return;
    }
    if (error) {
      out.push({ line, text: raw, value: null, status: "error", error, field });
      return;
    }
    const id = norm(spec.identity(row));
    if (id && seen.has(id)) {
      out.push({ line, text: raw, value: row, status: "duplicate" });
      return;
    }
    if (id) seen.add(id);
    out.push({ line, text: raw, value: row, status: "new" });
  });
  return out;
}

/** The rows a paste adds: its new lines, in the order pasted. */
export function pastedValues<V>(lines: readonly PasteLine<V>[]): V[] {
  return lines.flatMap((line) => (line.status === "new" && line.value ? [line.value] : []));
}
