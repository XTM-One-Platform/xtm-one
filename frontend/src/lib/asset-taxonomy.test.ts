// SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { describe, it } from "vitest";
import { ASSET_CATEGORY_LABELS, parseTypeValue, typeLabel, typeOptions, typeValue } from "./asset-taxonomy";

const id = (text: string) => text;

describe("asset taxonomy (#11781, OpenAEV's AssetCategory / AssetSubCategory)", () => {
  it("offers the 12 categories and their 80 subcategories, each under its category", () => {
    const options = typeOptions(id);
    assert.equal(Object.keys(ASSET_CATEGORY_LABELS).length, 12);
    assert.equal(options.filter((o) => !o.value.includes("/")).length, 12);
    assert.equal(options.filter((o) => o.value.includes("/")).length, 80);
    const db = options.find((o) => o.value === "cloud_resource/database");
    assert.equal(db?.group, "Cloud resource");
    assert.equal(db?.selectedLabel, "Cloud resource > Database (asset subcategory)");
  });

  it("round-trips a picker value and falls back to generic_asset on an unknown category", () => {
    assert.equal(typeValue("host", "server"), "host/server");
    assert.equal(typeValue("host", ""), "host");
    assert.deepEqual(parseTypeValue("host/server"), { category: "host", subcategory: "server" });
    assert.deepEqual(parseTypeValue("host"), { category: "host", subcategory: "" });
    assert.deepEqual(parseTypeValue("printer"), { category: "generic_asset", subcategory: "" });
  });

  it("labels a row as Category > Subcategory, or the category alone", () => {
    assert.equal(typeLabel(id, "network_device", "firewall"), "Network device > Firewall");
    assert.equal(typeLabel(id, "ai_target", ""), "AI system (asset category)");
  });
});
