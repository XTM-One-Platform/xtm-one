// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { isAssetNotFound } from "@/hooks/api/useOrganizationAssetsApi";
import { assetCategory, assetLabel } from "./inventory";

describe("assetCategory", () => {
  it("falls back to the category when the API answers an empty subcategory", () => {
    expect(assetCategory({ category: "server", subcategory: "" })).toBe("server");
    expect(assetLabel(assetCategory({ category: "web_application", subcategory: "" }))).toBe("Web application");
  });

  it("prefers the subcategory when there is one", () => {
    expect(assetCategory({ category: "server", subcategory: "database_server" })).toBe("database_server");
  });

  it("reads empty when neither is set", () => {
    expect(assetCategory({ category: null, subcategory: null })).toBe("");
  });
});

describe("isAssetNotFound", () => {
  it("is true for a 404 only", () => {
    expect(isAssetNotFound(Object.assign(new Error("Asset not found"), { status: 404 }))).toBe(true);
    expect(isAssetNotFound(Object.assign(new Error("boom"), { status: 500 }))).toBe(false);
    expect(isAssetNotFound(null)).toBe(false);
  });
});
