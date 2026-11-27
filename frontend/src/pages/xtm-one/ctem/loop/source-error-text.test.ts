// SPDX-License-Identifier: Apache-2.0

/**
 * #2637: a source in error says why, on which integration, when it was last tried and how to fix it, from the
 * path answer (UX round 1 on #2653, B1 and B2); a source switched off for a missing setting reads Not configured.
 */
import { describe, expect, it } from "vitest";
import type { CtemSignalPathSource } from "@/hooks/api/useCtemSignalsApi";
import { PLATFORM_SOURCE_FIX, PLATFORM_SOURCE_REASON, platformSourceFixRoute, sourceErrorLines, sourceReasonLine } from "./platform-sources";
import { sourceState } from "./signal-sources";

const t = (key: string, values?: Record<string, string | number>) =>
  Object.entries(values ?? {}).reduce((text, [k, v]) => text.split(`{${k}}`).join(String(v)), key);

const base: CtemSignalPathSource = {
  provider: "opencti",
  kind: "listener",
  key: "opencti:listener",
  name: "",
  default_on: true,
  event: "",
  bound: false,
} as CtemSignalPathSource;

type ErrorEntry = NonNullable<CtemSignalPathSource["errors"]>[number];

const entry = (over: Partial<ErrorEntry>): ErrorEntry => ({
  integration_id: "i-1",
  enabled: true,
  reason: "",
  reason_code: "run_as_inactive",
  reason_params: {},
  last_attempt_at: "2026-11-27T15:00:00Z",
  fix: "English fix the page never renders",
  fix_route: "/ctem/signals",
  ...over,
});

describe("source error lines (#2637, UX round 1 on #2653)", () => {
  const names = new Map([
    ["i-1", "Lab OpenCTI"],
    ["i-2", "Lab OpenCTI EU"],
  ]);

  it("gives one line per integration, named, with the reason, the fix and the last attempt", () => {
    const lines = sourceErrorLines(
      {
        errors: [
          entry({}),
          entry({ integration_id: "i-2", reason_code: "provider_error", reason_params: { detail: "OpenCTI answered 403 Forbidden" } }),
        ],
      },
      names,
      t,
      () => "Nov 27, 2026, 3:00 PM",
    );
    expect(lines).toHaveLength(2);
    expect(lines[0]).toMatch(/^Lab OpenCTI: The run-as user is deactivated/);
    expect(lines[0]).toContain("Last attempt Nov 27, 2026, 3:00 PM.");
    expect(lines[1]).toMatch(/^Lab OpenCTI EU: The source platform refused: OpenCTI answered 403 Forbidden\. /);
    expect(lines[1]).toContain(PLATFORM_SOURCE_FIX.provider_error);
    expect(lines.join(" ")).not.toContain("English fix the page never renders");
  });

  it("never repeats a fix the reason sentence already says", () => {
    const line = sourceReasonLine({ reason_code: "run_as_inactive", reason_params: {} }, null, t);
    expect(line).toBe(PLATFORM_SOURCE_REASON.run_as_inactive);
  });

  it("ends every reason with its full stop before the fix", () => {
    const line = sourceReasonLine({ reason_code: "provider_error", reason_params: { detail: "denied on this platform" } }, null, t);
    expect(line).toBe(`The source platform refused: denied on this platform. ${PLATFORM_SOURCE_FIX.provider_error}`);
  });

  it("reads without the name when the integration is unknown, and is empty without an error", () => {
    expect(sourceErrorLines({ errors: [entry({})] }, new Map(), t, () => "x")[0]).toMatch(/^The run-as user/);
    expect(sourceErrorLines({}, names, t, () => "")).toEqual([]);
  });

  it("links a fix done on another page only", () => {
    expect(platformSourceFixRoute({ fix_route: "/integrations" })).toBe("/integrations");
    expect(platformSourceFixRoute({ fix_route: "/ctem/signals" })).toBeNull();
    expect(platformSourceFixRoute({ fix_route: "https://evil.example" })).toBeNull();
  });

  it("has a fix for every reason code", () => {
    expect(Object.keys(PLATFORM_SOURCE_FIX).sort()).toEqual(Object.keys(PLATFORM_SOURCE_REASON).sort());
  });

  it("reads Not configured for a source off for a missing setting, Error only for a real error", () => {
    const connected = new Set(["opencti"]);
    expect(sourceState({ ...base, not_set_up: true, not_configured: true }, connected)).toBe("not_configured");
    expect(sourceState({ ...base, error_count: 1 }, connected)).toBe("error");
    expect(sourceState({ ...base, not_set_up: true }, connected)).toBe("not_set_up");
  });
});
