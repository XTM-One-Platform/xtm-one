// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import en from "@/i18n/lang/en.json";
import fr from "@/i18n/lang/fr.json";
import { isAssetNotFound } from "@/hooks/api/useOrganizationAssetsApi";
import { IMPORT_SOURCE_NAMES } from "@/lib/organization-profile";
import { SOURCE_KIND_LABELS } from "@/lib/organization-provenance";
import {
  assetCategory,
  assetCategoryLabel,
  assetFieldLabel,
  edgeOwnVersion,
  isCodeRepository,
  sourceLabel,
} from "./inventory";

type Catalog = Record<string, string>;
const translator = (catalog: Catalog) => (id: string) => catalog[id] ?? id;
const tEn = translator(en as Catalog);
const tFr = translator(fr as Catalog);

// UX round 3 on #1549, B1: the API names this software's version in from_version_id in both directions.
describe("edgeOwnVersion", () => {
  const openssl = new Map([["v-openssl-3011", "3.0.11"]]);

  it("renders the version of this software on a Used by edge, not Unknown", () => {
    const usedBy = { from_version_id: "v-openssl-3011", version_id: "v-libcurl-840", version: "8.4.0" };
    expect(edgeOwnVersion(usedBy, openssl)).toBe("3.0.11");
  });

  it("renders the version of this software on a Depends on edge", () => {
    const libcurl = new Map([["v-libcurl-840", "8.4.0"]]);
    const dependsOn = { from_version_id: "v-libcurl-840", version_id: "v-openssl-3011", version: "3.0.11" };
    expect(edgeOwnVersion(dependsOn, libcurl)).toBe("8.4.0");
  });

  it("reads unknown when the version is not one of this software's", () => {
    expect(edgeOwnVersion({ from_version_id: "v-other" }, openssl)).toBe("unknown");
  });
});

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
    expect(assetFieldLabel(tEn, "software_type", "operating_system")).toBe("Operating system");
    expect(assetFieldLabel(tEn, "cpe_state", "unresolved")).toBe("Not resolved");
    expect(assetFieldLabel(tEn, "origin", "manual")).toBe("Entered by hand");
    expect(assetFieldLabel(tEn, "environment", "production")).toBe("Production");
  });

  it("keeps an unknown value as stored and an empty one empty", () => {
    expect(assetFieldLabel(tEn, "criticality", "custom_level")).toBe("custom_level");
    expect(assetFieldLabel(tEn, "criticality", null)).toBe("");
    expect(assetCategoryLabel(tEn, { category: null, subcategory: null })).toBe("");
  });
});

// UX round 3 on #1254, B10: a record's source reads in the reader's language too.
describe("sourceLabel", () => {
  it("reads a CSV row in French", () => {
    expect(sourceLabel(tFr, "csv")).toBe("Import CSV");
    expect(sourceLabel(tFr, "manual")).toBe("Saisi à la main");
    expect(sourceLabel(tFr, "agent:triage")).toBe("triage");
  });

  it("names the known sources in English and keeps an unknown one as stored", () => {
    expect(sourceLabel(tEn, "csv")).toBe("CSV import");
    expect(sourceLabel(tEn, "opencti")).toBe("OpenCTI");
    expect(sourceLabel(tEn, "custom_feed")).toBe("custom_feed");
  });

  // UX round 6 on #1254, B12: the Sources tab chip and the list it lands on name a source the same way.
  it("names each hand and agent kind as the Sources and syncs table does", () => {
    for (const kind of ["manual", "csv", "api"] as const) expect(sourceLabel(tEn, kind)).toBe(SOURCE_KIND_LABELS[kind]);
    expect(sourceLabel(tEn, "manual")).toBe("Entered by hand");
  });

  // UX round 7 on #1254, B13: an agent source reads with the agent's name, the Sources tab's display_name of the key.
  it("names an agent source by its agent, so two agents give two labels", () => {
    expect(sourceLabel(tEn, "agent:triage")).toBe("triage");
    expect(sourceLabel(tEn, "agent:Triage agent")).toBe("Triage agent");
    const labels = ["agent:triage agent", "agent:exposure scout"].map((source) => sourceLabel(tEn, source));
    expect(new Set(labels).size).toBe(2);
    expect(labels).not.toContain(SOURCE_KIND_LABELS.agent);
    expect(sourceLabel(tEn, "agent:")).toBe(SOURCE_KIND_LABELS.agent);
  });

  // Review of #1254, P3: since #1262 asset rows carry `intune` and `defender`, named as the Sources and syncs table does.
  it("names the Microsoft endpoint sources by their product, in every language", () => {
    for (const t of [tEn, tFr]) {
      expect(sourceLabel(t, "intune")).toBe(IMPORT_SOURCE_NAMES.intune);
      expect(sourceLabel(t, "defender")).toBe(IMPORT_SOURCE_NAMES.defender);
    }
    expect(sourceLabel(tEn, "intune")).toBe("Microsoft Intune");
    expect(sourceLabel(tEn, "defender")).toBe("Microsoft Defender for Endpoint");
  });
});

describe("isCodeRepository", () => {
  it("is true only for a source code repository", () => {
    expect(isCodeRepository({ category: "source_code", subcategory: "code_repository" })).toBe(true);
    expect(isCodeRepository({ category: "source_code", subcategory: "ci_cd_pipeline" })).toBe(false);
    expect(isCodeRepository({ category: "server", subcategory: null })).toBe(false);
    expect(isCodeRepository(undefined)).toBe(false);
  });

  it("has the dependency labels in every catalog", () => {
    for (const key of ["Dependencies", "Package URL", "No dependency known in this repository yet."]) {
      expect((en as Record<string, string>)[key]).toBeTruthy();
      expect((fr as Record<string, string>)[key]).toBeTruthy();
    }
  });
});

describe("isAssetNotFound", () => {
  it("is true for a 404 only", () => {
    expect(isAssetNotFound(Object.assign(new Error("Asset not found"), { status: 404 }))).toBe(true);
    expect(isAssetNotFound(Object.assign(new Error("boom"), { status: 500 }))).toBe(false);
    expect(isAssetNotFound(null)).toBe(false);
  });
});
