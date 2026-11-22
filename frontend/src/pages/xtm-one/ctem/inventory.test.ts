// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import en from "@/i18n/lang/en.json";
import fr from "@/i18n/lang/fr.json";
import { isAssetNotFound } from "@/hooks/api/useOrganizationAssetsApi";
import { assetCategory, assetCategoryLabel, assetFieldLabel } from "./inventory";

type Catalog = Record<string, string>;
const translator = (catalog: Catalog) => (id: string) => catalog[id] ?? id;
const tEn = translator(en as Catalog);
const tFr = translator(fr as Catalog);

describe("assetCategory", () => {
  it("falls back to the category when the API answers an empty subcategory", () => {
    expect(assetCategory({ category: "server", subcategory: "" })).toBe("server");
  });

  it("prefers the subcategory when there is one", () => {
    expect(assetCategory({ category: "server", subcategory: "database_server" })).toBe("database_server");
  });

  it("reads empty when neither is set", () => {
    expect(assetCategory({ category: null, subcategory: null })).toBe("");
  });
});

// UX round 2 on #1254, B6: the cells read the shared taxonomy labels in the reader's language, never the stored enum.
describe("assetCategoryLabel and assetFieldLabel", () => {
  it("reads a host row in French", () => {
    expect(assetCategoryLabel(tFr, { category: "host", subcategory: "" })).toBe("Hôte");
    expect(assetFieldLabel(tFr, "criticality", "medium")).toBe("Moyenne");
    expect(assetFieldLabel(tFr, "internet_facing", "yes")).toBe("Oui");
  });

  it("names the taxonomy words in English, never a re-cased enum", () => {
    expect(assetCategoryLabel(tEn, { category: "ai_target", subcategory: "llm_model" })).toBe("AI system > LLM model");
    expect(assetCategoryLabel(tEn, { category: "saas_application", subcategory: "saas_app" })).toBe("SaaS application > SaaS app");
    expect(assetCategoryLabel(tEn, { category: "web_application", subcategory: "" })).toBe("Web application");
    expect(assetFieldLabel(tEn, "criticality", "very_high")).toBe("Very high");
    expect(assetFieldLabel(tEn, "kind", "installed")).toBe("Installed software");
    expect(assetFieldLabel(tEn, "environment", "production")).toBe("Production");
  });

  it("keeps an unknown value as stored and an empty one empty", () => {
    expect(assetFieldLabel(tEn, "criticality", "custom_level")).toBe("custom_level");
    expect(assetFieldLabel(tEn, "criticality", null)).toBe("");
    expect(assetCategoryLabel(tEn, { category: null, subcategory: null })).toBe("");
  });
});

describe("isAssetNotFound", () => {
  it("is true for a 404 only", () => {
    expect(isAssetNotFound(Object.assign(new Error("Asset not found"), { status: 404 }))).toBe(true);
    expect(isAssetNotFound(Object.assign(new Error("boom"), { status: 500 }))).toBe(false);
    expect(isAssetNotFound(null)).toBe(false);
  });
});
