// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { ownerBasisSentences, owningTeam } from "./case-owner-why";

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

describe("the owning team (#2586)", () => {
  const team = { id: "g-1", name: "Payments team" };
  const group = { kind: "asset_group" as const, name: "Payments servers", assets: 8, affected: 10, team };

  it("is the basis team while the case still belongs to it", () => {
    expect(owningTeam(group, "g-1")).toEqual(team);
    expect(owningTeam({ kind: "primary_asset", name: "portal", team }, "g-1")).toEqual(team);
  });

  it("is none once the owner was set by hand or the basis names another team", () => {
    expect(owningTeam(group, null)).toBeNull();
    expect(owningTeam(group, "g-2")).toBeNull();
    expect(owningTeam({ kind: "person" }, "g-1")).toBeNull();
    expect(owningTeam(null, "g-1")).toBeNull();
  });

  it("words the team as the owner and the person shown as its lead", () => {
    const sentences = ownerBasisSentences(group, t, team);
    expect(sentences[0]).toContain("The team Payments team owns the asset group Payments servers");
    expect(sentences[1]).toContain("leads the case for the team");
    const primary = ownerBasisSentences({ kind: "primary_asset", name: "portal", team }, t, team);
    expect(primary[0]).toContain("The team Payments team is accountable for the primary asset portal");
    expect(primary).toHaveLength(2);
  });

  it("keeps the person sentences without a team", () => {
    expect(ownerBasisSentences(group, t)).toHaveLength(1);
    expect(ownerBasisSentences(group, t)[0]).toMatch(/^Owner of the asset group Payments servers/);
  });
});
