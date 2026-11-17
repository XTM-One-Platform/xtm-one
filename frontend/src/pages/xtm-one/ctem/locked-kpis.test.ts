// D3-1 of the CTEM open-source program (#9425): with AI locked, the two
// agentic KPI slots show deterministic exposure metrics; with AI able to run,
// they stay the agentic cards.

import { describe, expect, it } from "vitest";

import { CTEM_METRIC_INFO } from "@/lib/constants";
import type { CtemVertical } from "@/types";
import en from "@/i18n/lang/en.json";
import { LOCKED_KPIS, lockedKpiFor } from "./locked-kpis";

const VERTICALS: CtemVertical[] = ["cyber", "fimi", "fraud"];

describe("lockedKpiFor", () => {
  it("keeps the agentic cards when AI can run", () => {
    for (const vertical of VERTICALS) {
      expect(lockedKpiFor(vertical, "kpi_agents", false)).toBeNull();
      expect(lockedKpiFor(vertical, "kpi_executions", false)).toBeNull();
    }
  });

  it("gives each agentic slot its deterministic KPI when AI is locked", () => {
    expect(lockedKpiFor("cyber", "kpi_agents", true)?.headlineKey).toBe("critical_count");
    expect(lockedKpiFor("cyber", "kpi_executions", true)?.headlineKey).toBe("exercised_coverage_pct");
    expect(lockedKpiFor("fimi", "kpi_agents", true)?.headlineKey).toBe("channels_total");
    expect(lockedKpiFor("fimi", "kpi_executions", true)?.headlineKey).toBe("incidents");
    expect(lockedKpiFor("fraud", "kpi_agents", true)?.headlineKey).toBe("credentials_total");
    expect(lockedKpiFor("fraud", "kpi_executions", true)?.headlineKey).toBe("containment_pct");
  });

  it("explains every locked KPI and translates its label and caption", () => {
    const catalog = en as Record<string, string>;
    for (const vertical of VERTICALS) {
      for (const kpi of LOCKED_KPIS[vertical]) {
        expect(CTEM_METRIC_INFO[kpi.label], kpi.label).toBeDefined();
        expect(catalog[kpi.label], kpi.label).toBeDefined();
        expect(catalog[kpi.sub], kpi.sub).toBeDefined();
      }
    }
  });
});
