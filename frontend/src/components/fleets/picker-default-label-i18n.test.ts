// The Fleet and Agent pickers on Operations > Executions are 160 px wide
// (TaskHistory), which leaves a 110 px text box at 14 px. Their
// default labels ("All fleets", "All Agents") must fit that box unclipped in
// every catalog: the long ja and es forms (141, 115 and 113 px) showed an
// ellipsis on load, and fr "Toutes les flottes" measured
// 111.9 px. These values were measured in the
// lab against the 110 px box; a new wording is measured there before it lands.

import { describe, expect, it } from "vitest";

import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";

type Catalog = Record<string, string>;

describe("Fleet and Agent picker default labels", () => {
  it("use the short forms measured to fit the 160 px pickers", () => {
    expect((ja as Catalog)["All Agents"]).toBe("全エージェント");
    expect((ja as Catalog)["All fleets"]).toBe("全フリート");
    expect((es as Catalog)["All Agents"]).toBe("Cualquier agente");
    expect((fr as Catalog)["All fleets"]).toBe("Toute flotte");
  });

  it("keep the en and fr forms that already fit", () => {
    expect((en as Catalog)["All Agents"]).toBe("All Agents");
    expect((fr as Catalog)["All Agents"]).toBe("Tous les agents");
  });
});
