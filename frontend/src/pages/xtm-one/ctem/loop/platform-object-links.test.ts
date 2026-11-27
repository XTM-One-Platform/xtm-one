// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import en from "@/i18n/lang/en.json";
import { PLATFORM_OBJECT_KIND_TEXT, PLATFORM_OBJECT_OPEN_LABEL, platformObjectLinks } from "./platform-sources";
import { SOURCE_KIND_LABEL } from "./signal-sources";

describe("Open in links of provisioned objects (#2583 point 2)", () => {
  it("opens an OpenCTI playbook by its id and the notifier and trigger lists, from the integration URL", () => {
    const links = platformObjectLinks({
      provider: "opencti",
      integration_url: "https://octi.example.com/",
      objects: [
        { kind: "notifier", id: "n1" },
        { kind: "trigger", id: "t1" },
        { kind: "playbook", id: "p 1" },
      ],
    });
    expect(links.map((l) => l.href)).toEqual([
      "https://octi.example.com/dashboard/settings/customization/notifiers",
      "https://octi.example.com/dashboard/profile/triggers",
      "https://octi.example.com/dashboard/data/processing/automation/p%201",
    ]);
  });

  it("opens the OpenAEV notifier and trigger lists under /admin", () => {
    const links = platformObjectLinks({
      provider: "openaev",
      integration_url: "https://oaev.example.com",
      objects: [
        { kind: "notifier", id: "n1" },
        { kind: "trigger", id: "t1" },
      ],
    });
    expect(links.map((l) => l.href)).toEqual([
      "https://oaev.example.com/admin/settings/customization/notifiers",
      "https://oaev.example.com/admin/profile/triggers",
    ]);
  });

  it("shows no link without an http(s) host, or for a provider without routes", () => {
    const objects = [{ kind: "trigger" as const, id: "t1" }];
    expect(platformObjectLinks({ provider: "opencti", integration_url: null, objects })).toEqual([]);
    expect(platformObjectLinks({ provider: "opencti", integration_url: "javascript:alert(1)", objects })).toEqual([]);
    expect(platformObjectLinks({ provider: "other", integration_url: "https://x.example.com", objects })).toEqual([]);
  });

  it("has an English key for every label it shows", () => {
    const keys = en as Record<string, string>;
    for (const label of Object.values(PLATFORM_OBJECT_OPEN_LABEL)) expect(keys[label]).toBe(label);
    // UX advisory A1 of #2592: each button writes the kind it opens; no two kinds share a text.
    const texts = Object.values(PLATFORM_OBJECT_KIND_TEXT);
    for (const text of texts) expect(keys[text]).toBe(text);
    expect(new Set(texts).size).toBe(texts.length);
    expect(keys[SOURCE_KIND_LABEL.cases_reader]).toBe("Cases recompute");
  });
});
