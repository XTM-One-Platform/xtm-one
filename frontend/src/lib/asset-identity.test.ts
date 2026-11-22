// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import { draftOf, moveItem, noKeyOn, savedOf } from "./asset-identity";

const defaults = {
  key_order: ["serial", "cloud_resource_id", "device_id", "mac", "fqdn", "hostname"],
  precedence: { os_version: ["mdm", "edr", "openaev", "hand", "*"], criticality: ["opencrq", "hand", "openaev", "opencti", "*"] },
};

describe("asset identity settings (#1178 slice 3)", () => {
  it("moves an item one place and leaves the ends alone", () => {
    expect(moveItem(["a", "b", "c"], 1, -1)).toEqual(["b", "a", "c"]);
    expect(moveItem(["a", "b", "c"], 0, -1)).toEqual(["a", "b", "c"]);
    expect(moveItem(["a", "b", "c"], 2, 1)).toEqual(["a", "b", "c"]);
  });

  it("drafts the keys in effect first, then the ones left out", () => {
    const draft = draftOf({ key_order: ["hostname", "serial"], precedence: defaults.precedence }, defaults);
    expect(draft.keys.slice(0, 2)).toEqual([
      { key: "hostname", on: true },
      { key: "serial", on: true },
    ]);
    expect(draft.keys.filter((entry) => !entry.on).map((entry) => entry.key)).toEqual(["cloud_resource_id", "device_id", "mac", "fqdn"]);
  });

  it("saves nothing when the draft equals the defaults, so the defaults keep applying", () => {
    expect(savedOf(draftOf(defaults, defaults), defaults)).toEqual({ key_order: [], precedence: {} });
  });

  it("saves only the key order and the fields that differ", () => {
    const draft = draftOf(defaults, defaults);
    draft.keys = moveItem(draft.keys, 5, -1);
    draft.precedence.criticality = moveItem(draft.precedence.criticality, 1, -1);
    expect(savedOf(draft, defaults)).toEqual({
      key_order: ["serial", "cloud_resource_id", "device_id", "mac", "hostname", "fqdn"],
      precedence: { criticality: ["hand", "opencrq", "openaev", "opencti", "*"] },
    });
  });

  it("drops a key switched off from the saved order and refuses an empty order", () => {
    const draft = draftOf(defaults, defaults);
    draft.keys = draft.keys.map((entry) => (entry.key === "hostname" ? { ...entry, on: false } : entry));
    expect(savedOf(draft, defaults).key_order).toEqual(["serial", "cloud_resource_id", "device_id", "mac", "fqdn"]);
    expect(noKeyOn(draft)).toBe(false);
    draft.keys = draft.keys.map((entry) => ({ ...entry, on: false }));
    expect(noKeyOn(draft)).toBe(true);
  });
});
