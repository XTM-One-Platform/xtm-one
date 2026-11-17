// The mobile companion is behind MOBILE_COMPANION: off, a phone gets the web
// app, so the companion shell, the PIN lock screen and the phone-only chat
// layout (every reader of this hook) never show.

import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const flag = vi.hoisted(() => ({ on: false }));

vi.mock("@/components/companion/companion-routes", () => ({ isPhoneDevice: () => true }));
vi.mock("@/hooks/useFeatureFlag", () => ({
  useFeatureFlag: (name: string) => name === "MOBILE_COMPANION" && flag.on,
}));

import { useCompanionDevice } from "./useCompanionDevice";

describe("useCompanionDevice on a phone", () => {
  it("is false while MOBILE_COMPANION is off", () => {
    flag.on = false;
    expect(renderHook(() => useCompanionDevice()).result.current).toBe(false);
  });

  it("is true once MOBILE_COMPANION is on", () => {
    flag.on = true;
    expect(renderHook(() => useCompanionDevice()).result.current).toBe(true);
  });
});
