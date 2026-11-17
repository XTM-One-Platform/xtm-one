import { describe, expect, it } from "vitest";

import { hostedFleetCount, liveHostedFleetCount } from "@/lib/workspace-hosted-fleets";
import type { Workspace } from "@/types";

const ws = (extra: Partial<Workspace>): Workspace => ({ id: "w", ...extra }) as Workspace;
const fleet = (id: string, is_archived = false) => ({ id, name: id, slug: id, is_archived });

describe("hosted fleet counts", () => {
  it("is zero for a workspace hosting nothing, or an older response", () => {
    expect(hostedFleetCount(ws({}))).toBe(0);
    expect(liveHostedFleetCount(ws({}))).toBe(0);
  });

  it("counts fleets the reader cannot see from the API counts", () => {
    const w = ws({ hosted_fleets: [], hosted_fleet_count: 2, live_hosted_fleet_count: 1 });
    expect(hostedFleetCount(w)).toBe(2);
    expect(liveHostedFleetCount(w)).toBe(1);
  });

  it("falls back on the named fleets, an archived one not live", () => {
    const w = ws({ hosted_fleets: [fleet("a"), fleet("b", true)] });
    expect(hostedFleetCount(w)).toBe(2);
    expect(liveHostedFleetCount(w)).toBe(1);
  });
});
