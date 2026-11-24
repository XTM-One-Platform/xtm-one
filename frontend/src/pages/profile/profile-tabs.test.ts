/**
 * The profile tabs (#822) - `yarn test`.
 *
 * A `/profile/:tab` segment that names no tab, the former `runners` included,
 * is not found instead of an empty profile.
 */
import { describe, expect, it } from "vitest";
import { isProfileTab } from "./profile-tabs";

describe("isProfileTab", () => {
  it("takes bare /profile and every tab", () => {
    expect(isProfileTab(undefined)).toBe(true);
    for (const tab of ["overview", "approvals", "channels", "phones", "agent-accounts", "memory", "quotas", "api-keys", "mcp"]) {
      expect(isProfileTab(tab)).toBe(true);
    }
  });

  it("refuses the former runners tab and any unknown segment", () => {
    for (const segment of ["runners", "desktop", "", "Overview", "nope"]) {
      expect(isProfileTab(segment)).toBe(false);
    }
  });
});
