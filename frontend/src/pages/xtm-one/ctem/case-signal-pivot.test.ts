// A case signal row from OpenCTI, OpenAEV or OpenCRQ opens its object in the module (#1357),
// the icon #1945 dropped; nothing for a non-module source, a module not connected, or no target.

import { describe, expect, it } from "vitest";
import type { CtemCaseVisibleIssue } from "@/hooks/api/useCtemCasesApi";
import { signalPivot } from "./case-issue-label";

const URLS = { opencti: "https://cti.example", openaev: "https://aev.example", opencrq: "https://crq.example" };

const signal = (over: Partial<CtemCaseVisibleIssue> = {}): CtemCaseVisibleIssue => ({
  source: "signal",
  external_id: "b4c4713e-62ec-427d-9013-cbc4822da4b4",
  open: true,
  name: "CVE-2023-28432",
  trigger_path: "new_vuln_running_version",
  lens: "world",
  source_provider: "opencti",
  source_external_id: "vulnerability--1",
  source_link: null,
  ...over,
});

describe("signalPivot", () => {
  it("opens an OpenCTI signal's entity by its id", () => {
    expect(signalPivot(signal(), URLS)).toEqual({ module: "opencti", href: "https://cti.example/dashboard/id/vulnerability--1" });
  });

  it("opens an OpenAEV signal's finding by its id, or the platform_url it carried", () => {
    expect(signalPivot(signal({ source_provider: "openaev", source_external_id: "f-1" }), URLS)).toEqual({
      module: "openaev",
      href: "https://aev.example/admin/findings/f-1",
    });
    expect(
      signalPivot(signal({ source_provider: "openaev", source_external_id: null, source_link: "https://aev.example/admin/scenarios/s-1" }), URLS)?.href,
    ).toBe("https://aev.example/admin/scenarios/s-1");
  });

  it("opens an OpenCRQ signal through the link its source delivered", () => {
    const link = "https://crq.example/t1/assets/a1/overview";
    expect(signalPivot(signal({ source_provider: "opencrq", source_external_id: null, source_link: link }), URLS)).toEqual({ module: "opencrq", href: link });
    // OpenCRQ has no id route XTM One builds: no link, no icon.
    expect(signalPivot(signal({ source_provider: "opencrq", source_external_id: "a1" }), URLS)).toBeNull();
  });

  it("draws nothing when the module is not connected", () => {
    expect(signalPivot(signal(), { openaev: URLS.openaev })).toBeNull();
    expect(signalPivot(signal({ source_provider: "opencrq", source_link: "https://crq.example/x" }), { opencti: URLS.opencti })).toBeNull();
  });

  it("draws nothing for a non-module source, an unsafe link or a product issue row", () => {
    expect(signalPivot(signal({ source_provider: "manual" }), URLS)).toBeNull();
    expect(signalPivot(signal({ source_provider: "listener" }), URLS)).toBeNull();
    expect(signalPivot(signal({ source_external_id: null, source_link: "javascript:alert(1)" }), URLS)).toBeNull();
    expect(signalPivot({ source: "opencti", external_id: "vulnerability--1", open: true }, URLS)).toBeNull();
  });
});
