// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { ownerBasisSentences } from "./case-owner-why";

// The ICU message is the key; this stub fills the plain placeholders, enough to read which sentence is chosen.
const t = (key: string, values?: Record<string, string | number>) =>
  key.replace(/\{(\w+)\}/g, (_, k: string) => String(values?.[k] ?? `{${k}}`));

describe("ownerBasisSentences (#2571)", () => {
  it("words an asset group owner with its share of the affected assets", () => {
    const [s] = ownerBasisSentences({ kind: "asset_group", name: "All MacOS x86_64", assets: 12, affected: 15 }, t);
    expect(s).toContain("All MacOS x86_64");
    expect(s).toContain("holds 12 of the");
  });

  it("words an asset group owner without the total when the API has none", () => {
    const [s] = ownerBasisSentences({ kind: "asset_group", name: "DNS", assets: 2, affected: null }, t);
    expect(s).toMatch(/^Owner of the asset group DNS, which holds \{assets, plural/);
  });

  it("names the primary asset", () => {
    expect(ownerBasisSentences({ kind: "primary_asset", name: "customer portal" }, t)[0]).toContain("customer portal");
  });

  it("tells where to set owners on every fallback, never on a person's choice", () => {
    const hint = "Set an owner on the asset groups in Inventory to route cases to the teams that fix them.";
    expect(ownerBasisSentences({ kind: "default" }, t)).toContain(hint);
    expect(ownerBasisSentences({ kind: "kept" }, t)).toContain(hint);
    const decider = ownerBasisSentences({ kind: "decider", source: "administrator", candidates: 3 }, t);
    expect(decider[0]).toContain("an administrator");
    expect(decider).toHaveLength(3);
    expect(decider[decider.length - 1]).toBe(hint);
    expect(ownerBasisSentences({ kind: "decider", source: "decide_group", candidates: 1 }, t)).toHaveLength(2);
    expect(ownerBasisSentences({ kind: "person" }, t)).toEqual(["Set by a person."]);
  });
});
