// SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { describe, it } from "vitest";
import { ASSET_CATEGORY_LABELS, parseTypeValue, typeLabel, typeOptions, typeValue } from "./asset-taxonomy";

const id = (text: string) => text;

describe("asset taxonomy (#11781, OpenAEV's AssetCategory / AssetSubCategory)", () => {
  it("offers the 13 categories and their 85 subcategories, each under its category", () => {
    const options = typeOptions(id);
    assert.equal(Object.keys(ASSET_CATEGORY_LABELS).length, 13);
    assert.equal(options.filter((o) => !o.value.includes("/")).length, 13);
    assert.equal(options.filter((o) => o.value.includes("/")).length, 85);
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

  it("offers Source code with its five subcategories, as the backend's ASSET_TAXONOMY (#1233)", () => {
    const sub = typeOptions(id)
      .filter((o) => o.value.startsWith("source_code/"))
      .map((o) => o.value);
    assert.deepEqual(sub, [
      "source_code/code_repository",
      "source_code/ci_cd_pipeline",
      "source_code/package_registry",
      "source_code/software_package",
      "source_code/code_other",
    ]);
    assert.deepEqual(parseTypeValue("source_code/code_repository"), { category: "source_code", subcategory: "code_repository" });
    assert.equal(typeLabel(id, "source_code", "code_repository"), "Source code > Code repository");
  });
});
