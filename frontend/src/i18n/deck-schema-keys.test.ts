// Gate: every message id the deck inspector shows from the block and slide
// schemas (a field label, a hint, an info bubble, an enum option's label and
// meaning line, an item list's label, a block's label) is a key of the four
// catalogs. The inspector translates them with `t_i18n(field.label)`, a key
// built at runtime that `validate:i18n` cannot read, so a missing key would
// render in English in every locale (UX round 1 of #10241: the poll block's
// "Show the QR code" and "Live, as votes arrive" in fr).

import { describe, expect, it } from "vitest";

import { BLOCK_SCHEMA, type SchemaField } from "@/components/presentation/block-schema";
import { SLIDE_SCHEMA } from "@/components/presentation/slide-schema";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";

type Shown = {
  label: string;
  hint?: string;
  info?: string;
  unsetHint?: string;
  optionLabels?: Readonly<Record<string, string>>;
  optionHints?: Readonly<Record<string, string>>;
  fields?: SchemaField[];
};

/** The message ids a field list shows, with where each comes from. */
function shownIds(fields: readonly SchemaField[], owner: string, out: Array<[string, string]>) {
  for (const field of fields as readonly Shown[]) {
    const where = `${owner} > ${field.label}`;
    out.push([where, field.label]);
    for (const id of [field.hint, field.info, field.unsetHint]) if (id) out.push([where, id]);
    for (const id of Object.values(field.optionLabels ?? {})) out.push([where, id]);
    for (const id of Object.values(field.optionHints ?? {})) out.push([where, id]);
    if (field.fields) shownIds(field.fields, where, out);
  }
  return out;
}

function allShownIds() {
  const ids: Array<[string, string]> = [];
  for (const [layout, schema] of Object.entries(SLIDE_SCHEMA)) {
    shownIds(schema.fields, `slide ${layout}`, ids);
    if (schema.items) {
      ids.push([`slide ${layout}`, schema.items.label]);
      shownIds(schema.items.fields, `slide ${layout} > ${schema.items.label}`, ids);
    }
  }
  for (const [type, schema] of Object.entries(BLOCK_SCHEMA)) {
    ids.push([`block ${type}`, schema.label]);
    shownIds(schema.fields, `block ${type}`, ids);
    if (schema.items) {
      ids.push([`block ${type}`, schema.items.label]);
      shownIds(schema.items.fields, `block ${type} > ${schema.items.label}`, ids);
    }
  }
  return ids;
}

const CATALOGS: Record<string, Record<string, string>> = { en, fr, es, ja };

describe("the deck inspector's schema strings", () => {
  it("are keys of every catalog", () => {
    const ids = allShownIds();
    expect(ids.length).toBeGreaterThan(100);
    const missing: string[] = [];
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      for (const [where, id] of ids) {
        if (!(id in catalog)) missing.push(`${locale}: "${id}" (${where})`);
      }
    }
    expect([...new Set(missing)]).toEqual([]);
  });
});
