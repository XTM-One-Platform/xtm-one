// The organization profile form (#8987): stored profile to editable text and back.
import { describe, it } from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_TICKETING_SYNC,
  formatAmount,
  fromForm,
  pirLink,
  pirNamesOf,
  splitList,
  isSupportedRecordType,
  ticketingDefaults,
  ticketingSyncOf,
  toForm,
  unreadablePlatform,
  type ProfileLike,
} from "./organization-profile.ts";

const PROFILE: ProfileLike = {
  name: "Acme",
  sector: "Finance",
  sub_sector: "",
  regions: ["FR", "DE"],
  size: "251-1000",
  crown_jewels: [{ name: "Payments", business_service: "Cards", owner: "CISO", criticality: "critical", opencrq_asset_id: "a-1" }],
  tech_stack: [],
  pir_ids: ["pir-1"],
  pir_names: { "pir-1": "Ransomware on energy" },
  risk_appetite: { annual_loss_threshold: 1000000, approval_threshold: null },
  loaded_hourly_rate: null,
  currency: "EUR",
  ticketing: { integration: "", project: "" },
};

describe("organization profile form", () => {
  it("round-trips a stored profile unchanged", () => {
    const result = fromForm(toForm(PROFILE));
    assert.ok("profile" in result);
    // The shipped products are always sent (#1432): an empty list is what clears them.
    assert.deepEqual(result.profile, { ...PROFILE, shipped_products: [] });
  });

  it("carries the shipped products through a save, drops rows without a product and repeats of a repository (#1432)", () => {
    const shipped: ProfileLike = {
      ...PROFILE,
      shipped_products: [{ product: "Acme Server", vendor: "Acme", cpe: "", repositories: ["acme/server"] }],
    };
    const kept = fromForm(toForm(shipped));
    assert.ok("profile" in kept);
    assert.deepEqual(kept.profile, shipped);
    const form = toForm(shipped);
    form.shipped_products = [
      { product: " ", vendor: "", cpe: "", repositories: [] },
      { product: "Acme Agent", vendor: "Acme", cpe: "", repositories: [" acme/agent ", "acme/agent", ""] },
    ];
    const cleaned = fromForm(form);
    assert.ok("profile" in cleaned);
    assert.deepEqual(cleaned.profile.shipped_products, [{ product: "Acme Agent", vendor: "Acme", cpe: "", repositories: ["acme/agent"] }]);
  });

  it("keeps the assets, the policy, the knowledge links and the crown jewel links through a save (#11543)", () => {
    const filled: ProfileLike = {
      ...PROFILE,
      crown_jewels: [{ ...PROFILE.crown_jewels[0], asset_names: ["web-1"] }],
      assets_exposure: [
        { name: "web-1", category: "host", subcategory: "", criticality: "unknown", environment: "production", internet_facing: "yes", owner: "", group: "", tech_stack: ["nginx"], tags: [], source: "csv", external_id: "" },
      ],
      prioritization_policy: {
        cvss_min: 7,
        epss_min: null,
        kev_always_in: true,
        internet_exposure_required: true,
        min_crown_jewel_criticality: "",
        exclusions: [],
        accepted_risks: [{ subject: "CVE-2024-1", scope: "", owner: "CISO", expires_on: "2027-01-31", reason: "" }],
      },
      enterprise_knowledge: [{ knowledge_base_id: "kb-1", label: "SOPs", kind: "sop" }],
    };
    const result = fromForm(toForm(filled));
    assert.ok("profile" in result);
    assert.deepEqual(result.profile, { ...filled, shipped_products: [] });
  });

  it("keeps the posture target through a save, and adds none to a profile without one (#11547 S2e)", () => {
    const withTarget: ProfileLike = { ...PROFILE, posture_target: 75 };
    const kept = fromForm(toForm(withTarget));
    assert.ok("profile" in kept);
    assert.equal(kept.profile.posture_target, 75);
    const none = fromForm(toForm(PROFILE));
    assert.ok("profile" in none);
    assert.equal("posture_target" in none.profile, false);
  });

  it("keeps a name only for the PIRs followed (#9615)", () => {
    assert.deepEqual(pirNamesOf(["pir-1", "pir-2"], { "pir-1": " A ", "pir-2": "", "pir-9": "Gone" }), { "pir-1": "A" });
  });

  it("links a PIR to its OpenCTI page, or nowhere without the address (#9615)", () => {
    assert.equal(pirLink("https://cti.example/", "a b"), "https://cti.example/dashboard/pirs/a%20b");
    assert.equal(pirLink("", "pir-1"), "");
  });

  it("keeps an empty amount empty, never 0", () => {
    const form = toForm(PROFILE);
    assert.equal(form.approval_threshold, "");
    assert.equal(form.loaded_hourly_rate, "");
  });

  it("splits, trims, upper-cases and de-duplicates regions", () => {
    assert.deepEqual(splitList(" fr, de;FR\nus ", true), ["FR", "DE", "US"]);
  });

  it("upper-cases and de-duplicates the picked regions and PIRs", () => {
    const form = toForm(PROFILE);
    form.regions = ["fr", "FR", "us"];
    form.pir_ids = ["pir-1", "pir-1", " "];
    const result = fromForm(form);
    assert.ok("profile" in result);
    assert.deepEqual(result.profile.regions, ["FR", "US"]);
    assert.deepEqual(result.profile.pir_ids, ["pir-1"]);
  });

  it("formats an amount with thousands separators, and leaves text that is not a number as is", () => {
    assert.equal(formatAmount("250000", "en-US"), "250,000");
    assert.equal(formatAmount("", "en-US"), "");
    assert.equal(formatAmount("12O", "en-US"), "12O");
  });

  it("drops rows with no name and normalises the currency", () => {
    const form = toForm(PROFILE);
    form.crown_jewels.push({ name: "  ", business_service: "", owner: "", criticality: "high", opencrq_asset_id: "" });
    form.currency = " usd ";
    const result = fromForm(form);
    assert.ok("profile" in result);
    assert.equal(result.profile.crown_jewels.length, 1);
    assert.equal(result.profile.currency, "USD");
  });

  it("names the platform of an unavailable line, never the backend's text", () => {
    assert.equal(unreadablePlatform("OpenCRQ: no OpenCRQ integration is connected"), "OpenCRQ");
    assert.equal(unreadablePlatform("OpenCTI PIRs: Enterprise Edition"), "OpenCTI PIRs");
    assert.equal(unreadablePlatform("OpenAEV: TimeoutError"), "OpenAEV");
    assert.equal(unreadablePlatform("something else"), "");
  });

  it("keeps the CTEM ticketing sync through a save, cleaned, and adds none to a profile without one (#917)", () => {
    const sync = { ...DEFAULT_TICKETING_SYNC, create_on: "mobilize_p1" as const, labels: ["ctem"], priority_map: { P1: "Highest" }, status_map: { "Ready for QA": "done" } };
    const withSync: ProfileLike = { ...PROFILE, ticketing: { integration: "int-1", project: "SEC", sync } };
    const kept = fromForm(toForm(withSync));
    assert.ok("profile" in kept);
    assert.deepEqual(kept.profile.ticketing, withSync.ticketing);
    const form = toForm(withSync);
    form.ticketing.sync = { ...sync, record_type: " Bug ", labels: ["ctem", " ctem", ""], priority_map: { P1: " ", P2: "High" }, status_map: { " ": "done", " QA ": "in_progress" } };
    const cleaned = fromForm(form);
    assert.ok("profile" in cleaned);
    assert.deepEqual(cleaned.profile.ticketing.sync, { ...sync, record_type: "Bug", labels: ["ctem"], priority_map: { P2: "High" }, status_map: { QA: "in_progress" } });
    const none = fromForm(toForm(PROFILE));
    assert.ok("profile" in none);
    assert.equal("sync" in none.profile.ticketing, false);
  });

  it("fills a partial stored sync with the program defaults, and names the backend default per provider (#917)", () => {
    assert.deepEqual(ticketingSyncOf({ on_reopen: "linked" }), { ...DEFAULT_TICKETING_SYNC, on_reopen: "linked" });
    assert.deepEqual(ticketingSyncOf(undefined), DEFAULT_TICKETING_SYNC);
    assert.equal(ticketingDefaults("servicenow").record_type, "incident");
    assert.equal(ticketingDefaults("jira").priority.P1, "Highest");
  });

  it("accepts only the three ServiceNow tables the case sync writes to, any Jira issue type", () => {
    for (const table of ["incident", "change_request", "problem", "", " problem "]) assert.equal(isSupportedRecordType("servicenow", table), true, table);
    for (const table of ["sc_task", "Incident", "task"]) assert.equal(isSupportedRecordType("servicenow", table), false, table);
    assert.equal(isSupportedRecordType("jira", "sc_task"), true);
    assert.equal(isSupportedRecordType("", "Bug"), true);
  });

  it("names the field when an amount is not a number", () => {
    const form = toForm(PROFILE);
    form.loaded_hourly_rate = "abc";
    assert.deepEqual(fromForm(form), { invalid: "Loaded hourly rate", field: "loaded_hourly_rate" });
    form.loaded_hourly_rate = "-5";
    assert.deepEqual(fromForm(form), { invalid: "Loaded hourly rate", field: "loaded_hourly_rate" });
    form.loaded_hourly_rate = "";
    form.approval_threshold = "12O";
    assert.deepEqual(fromForm(form), { invalid: "Escalation threshold", field: "approval_threshold" });
  });
});
