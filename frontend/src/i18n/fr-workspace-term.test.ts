/**
 * One French term for a workspace - `yarn test`.
 *
 * The Operations tab read "Espaces de travail" while the same page showed
 * "Nouveau workspace" and "Aucun workspace pour l'instant": 40 fr values said
 * "workspace", 65 said "espace de travail". The product term in fr is
 * "espace de travail", as es ("espacio de trabajo") and ja already write it.
 * This fails on any fr value that falls back to the English word, so the
 * catalog cannot drift into a mix again. "Google Workspace" is a product name
 * and stays; an ICU argument named {workspace} is code, not copy.
 */
import { expect, test } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const fr = JSON.parse(
  readFileSync(join(process.cwd(), "src", "i18n", "lang", "fr.json"), "utf8"),
) as Record<string, string>;

test("fr writes \"espace de travail\", never \"workspace\"", () => {
  const mixed = Object.entries(fr)
    .filter(([, message]) =>
      /\bworkspaces?\b/i.test(
        message.replace(/Google Workspace/g, "").replace(/\{[a-zA-Z_]+\}/g, ""),
      ),
    )
    .map(([key, message]) => `${key} => ${message}`);
  expect(mixed, "use \"espace de travail\" (one term per language)").toEqual([]);
});

test("the Operations tab and the workspace list read the same term", () => {
  expect(fr["Workspaces"]).toBe("Espaces de travail");
  expect(fr["New workspace"]).toBe("Nouvel espace de travail");
  expect(fr["No workspaces yet"]).toBe("Aucun espace de travail pour l'instant");
});
