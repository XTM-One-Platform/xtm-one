// Gate: every word the block inspector shows for a block (its label, its
// list label, each field's label, hint and option labels, nested object
// fields included) is a key of every catalog, so the inspector never falls
// back to English in fr, es or ja.

import { describe, expect, it } from "vitest";

import { BLOCK_SCHEMA, type SchemaField } from "@/components/presentation/block-schema";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";

type Worded = {
  label?: string;
  hint?: string;
  optionLabels?: Record<string, string>;
  fields?: readonly SchemaField[];
};

/** Every visible string of a field list, nested object fields included. */
function words(fields: readonly SchemaField[], out: Set<string> = new Set()) {
  for (const field of fields as readonly Worded[]) {
    if (field.label) out.add(field.label);
    if (field.hint) out.add(field.hint);
    for (const option of Object.values(field.optionLabels ?? {})) out.add(option);
    if (field.fields) words(field.fields, out);
  }
  return out;
}

/** Blocks whose inspector words are gated here (the blocks added with the gate). */
const GATED = ["formula", "config_cards"] as const;

const CATALOGS: Record<string, Record<string, string>> = { en, es, fr, ja };

describe("the block inspector words", () => {
  for (const type of GATED) {
    it(`are keys of every catalog for ${type}`, () => {
      const schema = BLOCK_SCHEMA[type];
      expect(schema).toBeDefined();
      const all = words(schema.fields);
      all.add(schema.label);
      if (schema.items) {
        all.add(schema.items.label);
        words(schema.items.fields, all);
      }
      expect(all.size).toBeGreaterThan(5);
      for (const [locale, catalog] of Object.entries(CATALOGS)) {
        const missing = [...all].filter((word) => !(word in catalog));
        expect([locale, missing]).toEqual([locale, []]);
      }
    });
  }
});
