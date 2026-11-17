/**
 * Every label the knowledge base Activity tab translates is a key of the four
 * catalogs: an event label, an event type of the filter, and the label of a
 * sync source with a translated name (the Source column).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { EVENT_CONFIG, EVENT_TYPE_LABELS } from "./kb-activity-events";
import { DOCUMENT_SOURCE_LABELS, sourceLabel } from "./sync-sources";

const LOCALES = ["en", "fr", "es", "ja"] as const;

function catalog(locale: string): Record<string, string> {
  return JSON.parse(readFileSync(join(process.cwd(), "src", "i18n", "lang", `${locale}.json`), "utf8")) as Record<string, string>;
}

const LABELS = [
  ...Object.values(EVENT_CONFIG).map((config) => config.label),
  ...Object.values(EVENT_TYPE_LABELS),
];

describe("knowledge base activity labels", () => {
  it.each(LOCALES)("every event and event type label is a %s catalog key", (locale) => {
    const strings = catalog(locale);
    expect(LABELS.filter((label) => !(label in strings))).toEqual([]);
  });

  it.each(LOCALES)("the Source column labels a translated source type in %s, never its raw key", (locale) => {
    const strings = catalog(locale);
    for (const type of ["website", "github", "github_public", "drata", ...Object.keys(DOCUMENT_SOURCE_LABELS)]) {
      const label = sourceLabel(type);
      expect(label).not.toBe(type);
      expect(label in strings).toBe(true);
    }
  });

  it("the event type filter options read as nouns in fr and es, never a verb", () => {
    const fr = catalog("fr");
    const es = catalog("es");
    expect(Object.values(EVENT_TYPE_LABELS).map((label) => fr[label])).toEqual(["Synchronisations", "Documents", "Embeddings"]);
    expect(Object.values(EVENT_TYPE_LABELS).map((label) => es[label])).toEqual(["Sincronizaciones", "Documentos", "Embeddings"]);
  });

  it("the total chip reads n of N when a search or an event type narrows the list", () => {
    const source = readFileSync(join(process.cwd(), "src", "pages", "knowledge-base-detail", "ActivityTab.tsx"), "utf8");
    expect(source).toMatch(/const \{ data: allCountData \} = useKBActivityCount\(kb\.id\);/);
    expect(source).toMatch(/totalAll=\{allCountData\?\.count\}/);
  });

  it("an unknown source type falls back to its key", () => {
    expect(sourceLabel("custom_source")).toBe("custom_source");
  });
});
