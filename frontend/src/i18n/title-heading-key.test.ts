// Gate: a document, slide, block or video title is labelled with the
// "Title (heading)" key, never with the bare "Title" key, which the catalogs
// translate as a person's job title (ja 役職, fr Fonction, es Cargo). The bare
// key stays on the fields that hold a person's position (speaker, people).

import { describe, expect, it } from "vitest";

import { BLOCK_SCHEMA, type SchemaField } from "@/components/presentation/block-schema";
import { SLIDE_SCHEMA } from "@/components/presentation/slide-schema";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";

type Labelled = { key: string; label: string; fields?: SchemaField[] };

/** Every `title` field label of a field list, with the label of the list that holds it. */
function titleLabels(fields: readonly SchemaField[], owner: string, out: Array<[string, string]> = []) {
  for (const field of fields as readonly Labelled[]) {
    if (field.key === "title") out.push([owner, field.label]);
    if (field.fields) titleLabels(field.fields, field.label, out);
  }
  return out;
}

/** The lists whose `title` is a person's position, not a heading. */
const PERSON_OWNERS = new Set(["Speaker", "Speakers", "People"]);

describe("the heading title key", () => {
  it("reads as a document title in every locale, apart from the job-title key", () => {
    expect(en["Title (heading)"]).toBe("Title");
    expect(fr["Title (heading)"]).toBe("Titre");
    expect(es["Title (heading)"]).toBe("Título");
    expect(ja["Title (heading)"]).toBe("タイトル");
    expect(ja["Title"]).toBe("役職");
  });

  it("labels every slide and block title field that is not a person's position", () => {
    const labels: Array<[string, string]> = [];
    for (const [layout, schema] of Object.entries(SLIDE_SCHEMA)) {
      titleLabels(schema.fields, layout, labels);
      if (schema.items) titleLabels(schema.items.fields, schema.items.label, labels);
    }
    for (const [type, schema] of Object.entries(BLOCK_SCHEMA)) {
      titleLabels(schema.fields, type, labels);
      if (schema.items) titleLabels(schema.items.fields, schema.items.label, labels);
    }
    expect(labels.length).toBeGreaterThan(10);
    for (const [owner, label] of labels) {
      expect([owner, label]).toEqual([owner, PERSON_OWNERS.has(owner) ? "Title" : "Title (heading)"]);
    }
  });
});
