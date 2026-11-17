import { describe, expect, it } from "vitest";

import { aiLockReason } from "@/hooks/useAiLockReason";

const base = { configLoaded: true, groupOff: false, platformOn: true, licensed: true, status: "ready" };

describe("aiLockReason", () => {
  it("answers null when AI can run", () => {
    expect(aiLockReason(base)).toBeNull();
    expect(aiLockReason({ ...base, status: "pending" })).toBeNull();
    expect(aiLockReason({ ...base, status: undefined })).toBeNull();
  });

  it("puts group AI off before everything", () => {
    expect(aiLockReason({ ...base, groupOff: true, platformOn: false, licensed: false, status: "no_model" })).toBe("group_off");
  });

  it("puts platform AI off before the license and the model", () => {
    expect(aiLockReason({ ...base, platformOn: false, licensed: false })).toBe("ai_off");
    expect(aiLockReason({ ...base, status: "ai_disabled" })).toBe("ai_off");
  });

  it("answers unlicensed in Community Edition, from the config or the ee_locked status", () => {
    expect(aiLockReason({ ...base, licensed: false })).toBe("unlicensed");
    expect(aiLockReason({ ...base, status: "ee_locked" })).toBe("unlicensed");
    expect(aiLockReason({ ...base, licensed: false, status: "no_model" })).toBe("unlicensed");
  });

  it("says nothing about the license while the config loads", () => {
    expect(aiLockReason({ ...base, configLoaded: false, licensed: false })).toBeNull();
  });

  it("answers no_model last, from the backend status alone", () => {
    expect(aiLockReason({ ...base, status: "no_model" })).toBe("no_model");
  });
});
