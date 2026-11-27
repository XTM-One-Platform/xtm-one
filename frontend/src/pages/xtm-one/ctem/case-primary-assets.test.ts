// #2635: a case states its inferred primary assets as inferred, never 0, and an
// unmatched case names what opened it.
import { describe, expect, it } from "vitest";

import { primaryAssetsCell, primaryAssetsLine } from "./case-primary-assets";

const t = (id: string, values?: Record<string, string | number>) => `${id}|${JSON.stringify(values ?? {})}`;
const fmt = (n: number | null) => (n == null ? "-" : String(n));

describe("primaryAssetsLine", () => {
  it("says the inferred primary assets as inferred when none is proven", () => {
    expect(primaryAssetsLine({ primary_asset_count: 0, inferred_primary_asset_count: 2, asset_count: 0 }, t)).toBe(
      `{count, plural, one {# inferred primary asset} other {# inferred primary assets}}|{"count":2}`,
    );
  });

  it("says both counts when some are proven and some inferred", () => {
    expect(primaryAssetsLine({ primary_asset_count: 1, inferred_primary_asset_count: 1, asset_count: 3 }, t)).toContain(`{"proven":1,"inferred":1}`);
  });

  it("keeps the proven count alone, 1 included", () => {
    expect(primaryAssetsLine({ primary_asset_count: 1, inferred_primary_asset_count: 0, asset_count: 1 }, t)).toBe(
      `{count, plural, one {# primary asset} other {# primary assets}}|{"count":1}`,
    );
  });

  it("names what opened a case that matched nothing", () => {
    const line = primaryAssetsLine({ primary_asset_count: 0, inferred_primary_asset_count: 0, asset_count: 0, opened_on: ["owned_domain", "unknown_kind"] }, t);
    expect(line).toBe(`No asset matched; opened on: {reasons}|{"reasons":"Owned domain|{}"}`);
  });

  it("is null when the count is unknown for this reader", () => {
    expect(primaryAssetsLine({ primary_asset_count: null, asset_count: null }, t)).toBeNull();
  });
});

describe("primaryAssetsCell", () => {
  it("shows the inferred count instead of 0", () => {
    expect(primaryAssetsCell({ primary_asset_count: 0, inferred_primary_asset_count: 3, asset_count: 0 }, t, fmt)).toBe(
      `{count, plural, one {# inferred} other {# inferred}}|{"count":3}`,
    );
  });

  it("keeps the proven count, and a null as missing", () => {
    expect(primaryAssetsCell({ primary_asset_count: 2, inferred_primary_asset_count: 3, asset_count: 0 }, t, fmt)).toBe("2");
    expect(primaryAssetsCell({ primary_asset_count: null, asset_count: null }, t, fmt)).toBe("-");
  });
});
