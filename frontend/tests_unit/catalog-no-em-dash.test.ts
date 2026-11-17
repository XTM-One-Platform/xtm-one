// @vitest-environment node
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

/**
 * No em dash in any catalog key or value, in en, fr, es and ja.
 *
 * House rule: user-facing copy never carries an em dash. The catalogs were swept
 * (a colon, a comma or a full stop in its place, never an en dash), and this guard
 * keeps a new one out: a key or a value with U+2014 fails here, with its locale and
 * key, rather than in a UX review. A string that must keep one goes in ALLOWED with
 * the reason; the list is empty and should stay so.
 */

const LANG = path.resolve(__dirname, "../src/i18n/lang");
const LOCALES = ["en", "fr", "es", "ja"] as const;
const EM_DASH = "\u2014";

// key -> reason. Applies to the key and to its value in every locale.
const ALLOWED: Record<string, string> = {};

function catalog(locale: string): Record<string, string> {
  return JSON.parse(readFileSync(path.join(LANG, `${locale}.json`), "utf8")) as Record<string, string>;
}

function emDashOffenders(locale: string, entries: Record<string, string>): string[] {
  return Object.entries(entries)
    .filter(([key, value]) => !(key in ALLOWED) && (key.includes(EM_DASH) || value.includes(EM_DASH)))
    .map(([key]) => `${locale}.json: ${JSON.stringify(key)}`);
}

describe("catalog copy carries no em dash", () => {
  it.each(LOCALES)("%s.json has no em dash in a key or a value", (locale) => {
    expect(emDashOffenders(locale, catalog(locale))).toEqual([]);
  });

  it("catches a seeded em dash in a key and in a translation", () => {
    expect(emDashOffenders("en", { "Embedding Provider \u2014 {provider}": "x" })).toHaveLength(1);
    expect(emDashOffenders("fr", { "Embedding Provider: {provider}": "Fournisseur \u2014 {provider}" })).toHaveLength(1);
    expect(emDashOffenders("ja", { "Embedding Provider: {provider}": "埋め込みプロバイダー：{provider}" })).toEqual([]);
  });

  it("every allow-listed key exists and gives a reason", () => {
    const en = catalog("en");
    for (const [key, reason] of Object.entries(ALLOWED)) {
      expect(en[key], key).toBeDefined();
      expect(reason.trim().length, key).toBeGreaterThan(10);
    }
  });

  it("the strings named in the issue read without an em dash", () => {
    const en = catalog("en");
    const fr = catalog("fr");
    const named = Object.keys(en).filter(
      (key) =>
        key.startsWith("Threat Intelligence Platform") ||
        key.startsWith("Embedding Provider") ||
        /^Configured target is \{dims\} dims/.test(key) ||
        /failed embedding/.test(key),
    );
    expect(named.length).toBeGreaterThanOrEqual(4);
    for (const key of named) {
      expect(key.includes(EM_DASH), key).toBe(false);
      expect(fr[key]?.includes(EM_DASH), `fr: ${key}`).toBe(false);
    }
  });

  it("two sentences that lost their em dash are split by a full stop, not a comma", () => {
    const catalogs = { en: catalog("en"), fr: catalog("fr"), es: catalog("es") };
    // A comma between two complete sentences reads wrong.
    const commaSplices: Record<keyof typeof catalogs, string[]> = {
      en: [
        "never run, the conversation moved on",
        "static token, this is the only scheme",
        "cut the client off, it is a key",
        "directly, download that one",
        "not available, sign in",
        "tiers, no ML dependency",
        "from Git, append",
        "never HTML, every",
        "surgically, one slide",
        "metadata, name, kind",
        // An aside that is itself a list goes in parentheses.
        "generate, reports, exports",
        "session, a printed runbook",
        "tools, reading mail",
        "every section, persona",
        "pins, tenant signature",
        "model, provider unreachable",
        "runs, reports, exports",
        "JSON answer, analysis",
        "action quota, only manually",
      ],
      fr: [
        "exécutés, la conversation",
        "exécuté, la conversation",
        "statique, c'est",
        "client, c'est une clé",
        "Plugins, téléchargez",
        "disponible, connectez-vous",
        "Git, ajoutez",
        "génèrent, rapports",
        "hors session, procédure",
        "service, lire des e-mails",
        "chaque section, persona",
        "configuration, signature du tenant",
        "modèle, provider injoignable",
        "exécutions, rapports",
        "JSON, analyses",
        "d'actions, seules",
      ],
      es: [
        "ejecutaron: la conversación",
        "ejecutó: la conversación",
        "Plugins, descargue",
        "disponible, inicie",
        "cliente, es una clave",
        "sesión, un runbook",
        "servicio, leer correo",
        "cada sección, herramientas",
        "modelo, proveedor inaccesible",
        "acciones, solo lo hacen",
      ],
    };
    const offenders: string[] = [];
    for (const [locale, fragments] of Object.entries(commaSplices) as [keyof typeof catalogs, string[]][]) {
      for (const [key, value] of Object.entries(catalogs[locale])) {
        if (fragments.some((fragment) => key.includes(fragment) || value.includes(fragment))) {
          offenders.push(`${locale}: ${key}`);
        }
      }
    }
    expect(offenders).toEqual([]);
    expect(catalogs.en["This tool needed your approval and was never run. The conversation moved on:"]).toBeDefined();
    expect(catalogs.en["VS Code needs no snippet: it reads the Agent Plugins package directly. Download that one instead."]).toBeDefined();
    expect(
      catalogs.en["Files you upload in a conversation and files your agents generate (reports, exports, decks) are collected here, wherever they were made."],
    ).toBeDefined();
    expect(
      catalogs.en["Platform agent: its scheduled runs do not consume the action quota. Only manually triggered runs do, and that is what this row counts."],
    ).toBeDefined();
  });

  // The mechanical replacement fused punctuation.
  // A colon before "including", "and" or "but" reads as a broken clause; a
  // semicolon fused with a colon is a leftover of a dash placeholder.
  const FUSED_COLON = /(;\s?:)|(\S\s?: (including|and|but) )|(\s:\s(y compris|et|mais) )|(: (incluid[oa]s?|y|pero) )/;
  // key -> reason. The fused pattern is allowed only where the colon belongs to a token.
  const FUSED_ALLOWED: Record<string, string> = {
    "Keyword tier only. Install the Nova engine (nova-hunting) to run the semantics: and llm: rule tiers. No ML dependency is added: they run on the platform's own models.":
      "semantics: and llm: are Nova rule-tier prefixes, not a clause break",
    "With the platform and the markings, as platform: and marking: tags":
      "platform: and marking: are the import's tag prefixes (#11886), not a clause break",
  };

  function fusedOffenders(locale: string, entries: Record<string, string>): string[] {
    return Object.entries(entries)
      .filter(([key, value]) => !(key in FUSED_ALLOWED) && (FUSED_COLON.test(key) || FUSED_COLON.test(value)))
      .map(([key]) => `${locale}.json: ${JSON.stringify(key)}`);
  }

  it.each(["en", "fr", "es", "ja"] as const)("%s.json has no colon fused before a conjunction and no ';:'", (locale) => {
    expect(fusedOffenders(locale, catalog(locale))).toEqual([]);
  });

  it("catches a seeded fused colon and honours the allow-list", () => {
    expect(fusedOffenders("en", { "The version the run used;: means the default.": "x" })).toHaveLength(1);
    expect(fusedOffenders("en", { "across the platform: including memories": "x" })).toHaveLength(1);
    expect(fusedOffenders("en", { "cannot change anything: but nobody": "x" })).toHaveLength(1);
    expect(fusedOffenders("fr", { k: "de la plateforme : y compris les mémoires" })).toHaveLength(1);
    expect(fusedOffenders("es", { k: "herramientas: incluidas sus herramientas" })).toHaveLength(1);
    expect(fusedOffenders("en", { "across the platform, including memories": "x" })).toEqual([]);
    const en = catalog("en");
    for (const [key, reason] of Object.entries(FUSED_ALLOWED)) {
      expect(en[key], key).toBeDefined();
      expect(reason.trim().length, key).toBeGreaterThan(10);
    }
  });

  // A ja list that lost its paired dashes runs into the sentence: "ファイル、A、B、C、は".
  // The list goes in full-width parentheses, as the other ja strings do.
  const JA_RUN_ON_LIST = /、[^、。（）"]{1,20}、[^、。（）"]{1,20}、[^、。（）"]{1,20}、(は|を)/;

  it("ja has no 3-item list running into the sentence and no comma splice in the named strings", () => {
    const ja = catalog("ja");
    const offenders = Object.entries(ja)
      .filter(([, value]) => JA_RUN_ON_LIST.test(value) || value.includes("制約です、") || value.includes("オプション、すべて"))
      .map(([key]) => key);
    expect(offenders).toEqual([]);
    expect(JA_RUN_ON_LIST.test("生成したファイル、レポート、エクスポート、スライド、は、どこで")).toBe(true);
    expect(JA_RUN_ON_LIST.test("成果物、レポート、エクスポート、データ、をひとつの")).toBe(true);
    expect(
      ja["Files you upload in a conversation and files your agents generate (reports, exports, decks) are collected here, wherever they were made."],
    ).toContain("ファイル（レポート、エクスポート、スライド）は");
    expect(ja["Uploads and outputs across conversations and runs (reports, exports, data), searchable in one repository."]).toContain(
      "成果物（レポート、エクスポート、データ）を",
    );
  });
});
