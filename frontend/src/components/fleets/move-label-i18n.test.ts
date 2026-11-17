// The "Move" key labels the fleet Move dialog's primary button (FleetMembersPanel)
// and the arcade controls legend (ArcadeOverlay). It is a transitive verb, "move
// this member", never a reflexive one: es "Moverse" ("to move oneself") and fr
// "Bouger" read wrong under "Mover a otra flota" / "Déplacer vers une autre
// flotte".

import { describe, expect, it } from "vitest";

import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";

type Catalog = Record<string, string>;

describe("Move label", () => {
  it("is the transitive verb in every catalog", () => {
    expect((es as Catalog)["Move"]).toBe("Mover");
    expect((fr as Catalog)["Move"]).toBe("Déplacer");
    expect((ja as Catalog)["Move"]).toBe("移動");
  });

  it("is the verb that opens the Move dialog's title in es and fr", () => {
    for (const catalog of [es, fr] as Catalog[]) {
      expect(catalog["Move to another fleet"].startsWith(`${catalog["Move"]} `)).toBe(true);
    }
  });
});
