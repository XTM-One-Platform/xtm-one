// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import { GATE_CARD_STATUS_LABEL, GATE_OUTCOME_LABEL } from "./case-gates";

const CATALOGS = [en, es, fr, ja] as Record<string, string>[];

/**
 * A gate that waited and was answered must not read as still waiting (#1539, UX round 3
 * on #1520 B1), and a gate card whose gate still waits must not read as past (UX round 1
 * on #1541 B1): History says the past event, the cards say the live status.
 */
describe("gate outcome copy (#1539)", () => {
  it("says the waited outcome apart from a gate still waiting, in every locale", () => {
    for (const catalog of CATALOGS) {
      expect(catalog[GATE_OUTCOME_LABEL.waited]).toBeTruthy();
      expect(catalog[GATE_OUTCOME_LABEL.waited]).not.toBe(catalog[GATE_CARD_STATUS_LABEL.waited]);
    }
    expect((fr as Record<string, string>)[GATE_OUTCOME_LABEL.waited]).not.toMatch(/^En attente/);
    expect((ja as Record<string, string>)[GATE_OUTCOME_LABEL.waited]).not.toMatch(/待ち$/);
  });

  it("gives a card whose gate still waits the present-tense status, in every locale", () => {
    expect(GATE_CARD_STATUS_LABEL.waited).toBe("Waiting for a person");
    for (const catalog of CATALOGS) {
      expect(catalog[GATE_CARD_STATUS_LABEL.waited]).toBeTruthy();
    }
    expect((fr as Record<string, string>)[GATE_CARD_STATUS_LABEL.waited]).toMatch(/^En attente/);
    expect((ja as Record<string, string>)[GATE_CARD_STATUS_LABEL.waited]).toMatch(/待ち$/);
    expect((es as Record<string, string>)[GATE_CARD_STATUS_LABEL.waited]).toMatch(/^Esperando/);
  });
});
