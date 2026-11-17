// A group that switches AI off reaches the whole interface through
// `useAiEnabled()`, the one reading every AI affordance asks (nav locks,
// disabled actions, the landing). The platform's own switch stays readable
// apart from it, for the screen that words the two differently.

import { describe, expect, it } from "vitest";
import { renderHook } from "@testing-library/react";

import type { User } from "@/types";
import { testQueryClient, withProviders } from "@/testing/providers";
import { useAiDisabledGroup } from "./useAiDisabledGroup";
import { useAiEnabled, usePlatformAiEnabled } from "./usePlatformConfig";

function render(me: Partial<User> | undefined) {
  const client = testQueryClient();
  if (me) client.setQueryData(["me"], me);
  return renderHook(
    () => ({
      aiEnabled: useAiEnabled(),
      platformOn: usePlatformAiEnabled(),
      group: useAiDisabledGroup(),
    }),
    { wrapper: withProviders(client) },
  );
}

describe("useAiDisabledGroup", () => {
  it("leaves AI on for a person no group switches off", () => {
    const { result } = render({ ai_disabled_by_group: false, ai_disabled_group: null });
    expect(result.current).toEqual({ aiEnabled: true, platformOn: true, group: null });
  });

  it("switches AI off for a member of a disabling group, the platform switch left on", () => {
    const { result } = render({ ai_disabled_by_group: true, ai_disabled_group: "Legal" });
    expect(result.current).toEqual({ aiEnabled: false, platformOn: true, group: "Legal" });
  });

  it("stays silent, and never fetches the profile, before a session exists", () => {
    const { result } = render(undefined);
    expect(result.current).toEqual({ aiEnabled: true, platformOn: true, group: null });
  });

  it("reads a group the server did not name as an empty name, still disabled", () => {
    const { result } = render({ ai_disabled_by_group: true, ai_disabled_group: null });
    expect(result.current.group).toBe("");
    expect(result.current.aiEnabled).toBe(false);
  });
});
