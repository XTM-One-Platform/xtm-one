import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { fixFrenchSpacing, frenchSpacingProblems } from "./french-spacing.mjs";

test("a plain space before ? ! ; is reported and fixed with a narrow no-break space", () => {
  const catalog = { a: "Nettoyer les exécutions bloquées ?", b: "Bravo !", c: "Un ; deux" };
  assert.deepEqual(frenchSpacingProblems(catalog), [
    { key: "a", mark: "?" },
    { key: "b", mark: "!" },
    { key: "c", mark: ";" },
  ]);
  assert.equal(fixFrenchSpacing(catalog.a), "Nettoyer les exécutions bloquées\u202f?");
  assert.equal(fixFrenchSpacing(catalog.c), "Un\u202f; deux");
});

test("a plain space before : is fixed with a no-break space, inside an ICU branch too", () => {
  const message = "{count, plural, one {Dernière cause :} other {Dernières causes :}}";
  assert.equal(frenchSpacingProblems({ m: message }).length, 2);
  assert.equal(fixFrenchSpacing(message), "{count, plural, one {Dernière cause\u00a0:} other {Dernières causes\u00a0:}}");
});

test("a mark that is not punctuation ending a word is left alone", () => {
  const catalog = {
    help: "Rechercher une page, ou taper > pour les commandes, ? pour l'aide",
    ipv6: "venant de cette machine (127.0.0.1, ::1)",
    uri: "une URI otpauth:// ; les codes",
    done: "Supprimer l'entrée\u202f?",
  };
  assert.deepEqual(frenchSpacingProblems(catalog), []);
});

test("the fr catalog has no plain space before a high punctuation mark", () => {
  const fr = JSON.parse(readFileSync(new URL("../../src/i18n/lang/fr.json", import.meta.url), "utf8"));
  assert.deepEqual(frenchSpacingProblems(fr), []);
});
