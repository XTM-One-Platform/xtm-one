// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import { GATE_OUTCOME_LABEL } from "./case-gates";

/**
 * A gate that waited and was answered must not read as still waiting (#1539, UX round 3
 * on #1520 B1): the waited outcome never shares its words with "Waiting for a person".
 */
describe("gate outcome copy (#1539)", () => {
  it("says the waited outcome apart from a gate still waiting, in every locale", () => {
    for (const catalog of [en, es, fr, ja] as Record<string, string>[]) {
      expect(catalog[GATE_OUTCOME_LABEL.waited]).toBeTruthy();
      expect(catalog[GATE_OUTCOME_LABEL.waited]).not.toBe(catalog["Waiting for a person"]);
    }
    expect((fr as Record<string, string>)[GATE_OUTCOME_LABEL.waited]).not.toMatch(/^En attente/);
    expect((ja as Record<string, string>)[GATE_OUTCOME_LABEL.waited]).not.toMatch(/待ち$/);
  });
});
