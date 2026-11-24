// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import {
  boundedInteger,
  cleanDomains,
  cleanRanges,
  DEFAULT_ATTACK_SURFACE,
  fromForm,
  invalidAddressRanges,
  normalizeAddressRange,
  normalizeDomain,
  passIntegrationIds,
  toForm,
  withIntegrationIds,
} from "./organization-profile";

/** The outside lens fields (CTEM-26 slice 2, #1140): normalised as the backend does, carried by every save. */
describe("address ranges, as normalize_address_ranges stores them", () => {
  it.each([
    ["198.51.100.7", "198.51.100.7/32"],
    ["198.51.100.7/24", "198.51.100.0/24"],
    [" 10.0.0.0/8 ", "10.0.0.0/8"],
    ["192.0.2.0/255.255.255.0", "192.0.2.0/24"],
    ["2001:db8::1", "2001:db8::1/128"],
    ["2001:DB8::/32", "2001:db8::/32"],
    ["2001:db8:0:0:1:0:0:1/128", "2001:db8::1:0:0:1/128"],
    ["::1", "::1/128"],
  ])("%s -> %s", (raw, stored) => {
    expect(normalizeAddressRange(raw)).toBe(stored);
  });

  it.each(["", "corp.example", "300.1.1.1", "10.0.0.0/33", "2001:db8::/129", "1.2.3", "01.2.3.4", "1.2.3.4/24/1", "2001:db8:::1"])(
    "refuses %s",
    (raw) => {
      expect(normalizeAddressRange(raw)).toBeNull();
    },
  );

  it("lists the refused entries as typed and dedupes the rest", () => {
    expect(invalidAddressRanges(["10.0.0.1", "nope", " "])).toEqual(["nope"]);
    expect(cleanRanges(["10.0.0.1", "10.0.0.1/32", "nope"])).toEqual(["10.0.0.1/32"]);
  });
});

describe("owned domains and the pass numbers", () => {
  it("stores a domain as the backend does", () => {
    expect(normalizeDomain(" @Corp.Example. ")).toBe("corp.example");
    expect(normalizeDomain("  ")).toBeNull();
    expect(cleanDomains(["corp.example", "CORP.example.", ""])).toEqual(["corp.example"]);
  });

  it("takes whole numbers in the bounds only", () => {
    expect(boundedInteger("24", { min: 1, max: 720 })).toBe(24);
    expect(boundedInteger("0", { min: 1, max: 720 })).toBeNull();
    expect(boundedInteger("721", { min: 1, max: 720 })).toBeNull();
    expect(boundedInteger("1.5", { min: 1, max: 720 })).toBeNull();
  });
});

describe("a profile save keeps the outside lens", () => {
  it("carries owned_domains, address_ranges and attack_surface through the form", () => {
    const profile = {
      name: "Acme",
      sector: "",
      sub_sector: "",
      regions: [],
      size: "",
      primary_assets: [],
      tech_stack: [],
      pir_ids: [],
      risk_appetite: { annual_loss_threshold: null, approval_threshold: null },
      loaded_hourly_rate: null,
      currency: "EUR",
      ticketing: { integration: "", project: "" },
      owned_domains: ["corp.example"],
      address_ranges: ["198.51.100.0/24"],
      attack_surface: { enabled: true, integration: "int-1", integrations: ["int-2", "int-3"], cadence_hours: 12, stale_after_days: 7 },
    };
    const saved = fromForm(toForm(profile));
    expect("profile" in saved && saved.profile.owned_domains).toEqual(["corp.example"]);
    expect("profile" in saved && saved.profile.address_ranges).toEqual(["198.51.100.0/24"]);
    expect("profile" in saved && saved.profile.attack_surface).toEqual(profile.attack_surface);
  });
});

describe("the providers of one pass (#1154: integration first, then integrations)", () => {
  it("reads integration then integrations, without blanks or repeats", () => {
    expect(passIntegrationIds({ integration: "a", integrations: ["b", "a", " ", "c"] })).toEqual(["a", "b", "c"]);
    expect(passIntegrationIds({ integration: "", integrations: undefined })).toEqual([]);
  });

  it("writes the first id in integration and the rest in integrations", () => {
    expect(withIntegrationIds(DEFAULT_ATTACK_SURFACE, ["x", "y", "z"])).toMatchObject({ integration: "x", integrations: ["y", "z"] });
    expect(withIntegrationIds({ ...DEFAULT_ATTACK_SURFACE, integration: "x" }, [])).toMatchObject({ integration: "", integrations: [] });
  });
});
