// A case member is named, never shown by its raw id (#1936).

import { describe, expect, it } from "vitest";
import type { CtemCaseVisibleIssue } from "@/hooks/api/useCtemCasesApi";
import { UUID_PATTERN, eventIssueLabel, issueLabel, memberAssetCount, memberKev, signalCaption } from "./case-issue-label";

const t = (key: string, values?: Record<string, string | number>) =>
  Object.entries(values ?? {}).reduce((text, [k, v]) => text.replace(`{${k}}`, String(v)), key);

const ID = "b4c4713e-62ec-427d-9013-cbc4822da4b4";
const signal = (over: Partial<CtemCaseVisibleIssue> = {}): CtemCaseVisibleIssue => ({
  source: "signal",
  external_id: ID,
  open: true,
  name: "O365 Device Code Phishing Campaign",
  trigger_path: "relevant_threat",
  trigger_path_label: "Relevant new threat or actor",
  lens: "world",
  source_provider: "opencti",
  subject_kind: "threat",
  assets: [],
  ...over,
});

describe("case issue labels", () => {
  it("names a signal row by its subject, its path as the caption", () => {
    expect(issueLabel(signal(), t)).toBe("O365 Device Code Phishing Campaign");
    expect(signalCaption(signal(), t)).toBe("Relevant new threat or actor");
  });

  it("falls back to the trigger path, never the id", () => {
    const named = [
      issueLabel(signal({ name: undefined }), t),
      issueLabel(signal({ name: ID }), t),
      issueLabel(signal({ name: undefined, trigger_path_label: null }), t),
      issueLabel({ source: "opencti", external_id: ID, open: true }, t),
    ];
    expect(named).toEqual(["Signal: Relevant new threat or actor", "Signal: Relevant new threat or actor", "Signal", ""]);
    for (const label of named) expect(label).not.toMatch(UUID_PATTERN);
  });

  it("leaves KEV and Assets empty on a signal unless it names an asset", () => {
    expect(memberKev(signal())).toBeNull();
    expect(memberAssetCount(signal())).toBeNull();
    expect(memberAssetCount(signal({ subject_kind: "asset", assets: ["portal-edge-01"] }))).toBe(1);
    expect(memberKev({ source: "opencti", external_id: "x", open: true })).toBe(false);
    expect(memberAssetCount({ source: "opencti", external_id: "x", open: true })).toBe(0);
  });

  it("names the signal of a History event, never its id", () => {
    const issue = `signal:${ID}`;
    expect(eventIssueLabel({ issue, issue_label: "O365 Device Code Phishing Campaign" }, t)).toBe("O365 Device Code Phishing Campaign");
    expect(eventIssueLabel({ issue, issue_label: null }, t)).toBe("A signal");
    expect(eventIssueLabel({ issue: `opencti:${ID}`, issue_label: null }, t)).toBeNull();
    expect(eventIssueLabel({ issue: "Restricted", issue_label: null }, t)).toBeNull();
    expect(eventIssueLabel({ issue: "opencti:CVE-2024-1234", issue_label: "CVE-2024-1234" }, t)).toBe("CVE-2024-1234");
  });
});
