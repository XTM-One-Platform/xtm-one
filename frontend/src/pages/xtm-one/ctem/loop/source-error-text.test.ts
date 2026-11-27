// SPDX-License-Identifier: Apache-2.0

/**
 * #2637: a source in error says why, when it was last tried and how to fix it, from the path answer;
 * a source switched off for a missing setting reads Not configured, never Error.
 */
import { describe, expect, it } from "vitest";
import type { CtemSignalPathSource } from "@/hooks/api/useCtemSignalsApi";
import { sourceErrorText } from "./platform-sources";
import { sourceState } from "./signal-sources";

const t = (key: string, values?: Record<string, string | number>) =>
  Object.entries(values ?? {}).reduce((text, [k, v]) => text.replace(`{${k}}`, String(v)), key);

const base: CtemSignalPathSource = {
  provider: "opencti",
  kind: "listener",
  key: "opencti:listener",
  name: "",
  default_on: true,
  event: "",
  bound: false,
} as CtemSignalPathSource;

describe("source error text (#2637)", () => {
  it("names the reason with its fix and the last attempt of each integration in error", () => {
    const text = sourceErrorText(
      {
        errors: [
          {
            integration_id: "i-1",
            enabled: true,
            reason: "the run-as user is deactivated",
            reason_code: "run_as_inactive",
            reason_params: {},
            last_attempt_at: "2026-11-27T15:00:00Z",
            fix: "Pick another run-as user.",
            fix_route: "/ctem/signals",
          },
        ],
      },
      t,
      () => "27 Nov 2026, 16:00",
    );
    expect(text).toContain("Pick another run-as user.");
    expect(text).toContain("Last attempt 27 Nov 2026, 16:00.");
  });

  it("is empty without an error", () => {
    expect(sourceErrorText({}, t, () => "")).toBe("");
  });

  it("reads Not configured for a source off for a missing setting, Error only for a real error", () => {
    const connected = new Set(["opencti"]);
    expect(sourceState({ ...base, not_set_up: true, not_configured: true }, connected)).toBe("not_configured");
    expect(sourceState({ ...base, error_count: 1 }, connected)).toBe("error");
    expect(sourceState({ ...base, not_set_up: true }, connected)).toBe("not_set_up");
  });
});
