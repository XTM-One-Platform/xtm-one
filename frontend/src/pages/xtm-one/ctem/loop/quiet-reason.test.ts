// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { quietReasonText } from "./quiet-reason";

// The catalog is not loaded here: the translator fills the values in the English key, plural included.
const t = (key: string, values: Record<string, string | number> = {}) => {
  let text = key.replace(/\{count, plural, one \{(.*?)\} other \{(.*)\}\}$/, (_m, one: string, other: string) =>
    (values.count === 1 ? one : other).replace(/#/g, String(values.count)),
  );
  for (const [k, v] of Object.entries(values)) text = text.split(`{${k}}`).join(String(v));
  return text;
};
const date = (iso: string) => `D(${iso.slice(0, 10)})`;

describe("quietReasonText (#2634, UX round 1 on #2654)", () => {
  it("words a code with the pass date in the locale format, never the raw timestamp", () => {
    const text = quietReasonText({ code: "no_undeclared_host", at: "2026-11-27T16:21:34.624416+00:00" }, t, date);
    expect(text).toBe("The last pass (D(2026-11-27)) found no undeclared host and no new port");
    expect(text).not.toContain("T16:21");
  });

  it("counts hosts with a real plural", () => {
    expect(quietReasonText({ code: "withheld", at: "2026-11-27T00:00:00Z", count: 1 }, t, date)).toContain("withheld 1 new host:");
    expect(quietReasonText({ code: "rearmed", at: "2026-11-27T00:00:00Z", count: 3 }, t, date)).toMatch(/^3 hosts re-armed/);
  });

  it("names the setting by its label, never its key", () => {
    const text = quietReasonText({ code: "pass_off" }, t, date) ?? "";
    expect(text).toContain("Attack surface, in the organization profile");
    expect(text).not.toContain("attack_surface.enabled");
  });

  it("adds the scopes the provider answered nothing for", () => {
    const text = quietReasonText({ code: "nothing_new", at: "2026-11-27T00:00:00Z", empty_scopes: ["opencrq.io"] }, t, date);
    expect(text).toBe("The last pass (D(2026-11-27)) found nothing new on this path; the provider answered nothing for opencrq.io");
  });

  it("shows nothing for no reason or a code this build does not know", () => {
    expect(quietReasonText(undefined, t, date)).toBeUndefined();
    expect(quietReasonText({ code: "later_code" }, t, date)).toBeUndefined();
  });
});
