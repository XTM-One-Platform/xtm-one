import { describe, expect, it } from "vitest";
import type { CtemBriefingResponse, CtemStageAssessmentResponse } from "@/types";
import { assessmentBusy, briefingBusy, briefingShown, postureBusy, shouldAutoGenerateBriefing } from "./landing-refresh";

const briefing = (over: Partial<CtemBriefingResponse>): CtemBriefingResponse =>
  ({ briefing: null, status: "ready", ...over }) as CtemBriefingResponse;

describe("landing refresh (#12183)", () => {
  it("generates a missing or stale briefing on landing, never a fresh one", () => {
    expect(shouldAutoGenerateBriefing(briefing({ status: "pending" }))).toBe(true);
    expect(shouldAutoGenerateBriefing(briefing({ status: "ready", stale: true }))).toBe(true);
    expect(shouldAutoGenerateBriefing(briefing({ status: "ready", stale: false }))).toBe(false);
    expect(shouldAutoGenerateBriefing(undefined)).toBe(false);
  });

  it("starts no second run while one is in flight", () => {
    expect(shouldAutoGenerateBriefing(briefing({ status: "generating" }))).toBe(false);
    expect(shouldAutoGenerateBriefing(briefing({ status: "ready", stale: true, generating: true }))).toBe(false);
  });

  it("never generates nor spins when the AI is locked", () => {
    for (const status of ["ee_locked", "ai_disabled", "no_model", "unavailable"] as const) {
      expect(shouldAutoGenerateBriefing(briefing({ status, stale: true }))).toBe(false);
      expect(briefingBusy(briefing({ status, generating: true }), false)).toBe(false);
      expect(assessmentBusy({ status, generating: true } as CtemStageAssessmentResponse)).toBe(false);
    }
  });

  it("keeps the stored briefing on screen while a run regenerates it (#12217)", () => {
    const stored = { headline: "Two exposed services" } as CtemBriefingResponse["briefing"];
    // (a) generating with a stored stale briefing: the stored one, never the Sample.
    const regenerating = briefing({ status: "generating", briefing: stored, stale: true, generating: true });
    expect(briefingShown(regenerating)).toBe(true);
    expect(briefingBusy(regenerating, false)).toBe(true);
    // (b) generating with nothing stored: the first-run state (the Sample).
    expect(briefingShown(briefing({ status: "generating", briefing: null }))).toBe(false);
    // (c) ready: unchanged.
    expect(briefingShown(briefing({ status: "ready", briefing: stored }))).toBe(true);
    expect(briefingShown(briefing({ status: "pending" }))).toBe(false);
    expect(briefingShown(undefined)).toBe(false);
    // A locked AI never shows a stored briefing as current.
    expect(briefingShown(briefing({ status: "ai_disabled", briefing: stored }))).toBe(false);
  });

  it("spins the posture while it is first built, rebuilt or forced", () => {
    expect(postureBusy(undefined, { loading: true, forcing: false })).toBe(true);
    expect(postureBusy({ refreshing: true }, { loading: false, forcing: false })).toBe(true);
    expect(postureBusy({ refreshing: false }, { loading: false, forcing: true })).toBe(true);
    expect(postureBusy({ refreshing: false }, { loading: false, forcing: false })).toBe(false);
    // A failed first read (no data, not loading) never spins forever.
    expect(postureBusy(undefined, { loading: false, forcing: false })).toBe(false);
  });

  it("spins a stage while its assessment is generated", () => {
    expect(assessmentBusy({ status: "generating" } as CtemStageAssessmentResponse)).toBe(true);
    expect(assessmentBusy({ status: "ready", generating: true } as CtemStageAssessmentResponse)).toBe(true);
    expect(assessmentBusy({ status: "ready" } as CtemStageAssessmentResponse)).toBe(false);
  });
});
