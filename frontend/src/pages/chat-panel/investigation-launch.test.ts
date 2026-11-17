// The Deep Investigation mode survives exactly one change of conversation: the
// one a `?investigate=` launch makes, from the conversation it left to its new
// chat. Every other change turns it off.

import { describe, expect, it } from "vitest";

import { keepsInvestigationMode } from "@/pages/chat-panel/investigation-launch";

describe("keepsInvestigationMode", () => {
  it("keeps the mode on for the launch's own change, to its new chat", () => {
    expect(keepsInvestigationMode({ left: "c-1", next: null, launchLeft: "c-1" })).toBe(true);
  });

  it("turns it off for any change without a launch", () => {
    expect(keepsInvestigationMode({ left: "c-1", next: null, launchLeft: null })).toBe(false);
    expect(keepsInvestigationMode({ left: "c-1", next: "c-2", launchLeft: null })).toBe(false);
    expect(keepsInvestigationMode({ left: null, next: "c-2", launchLeft: null })).toBe(false);
  });

  it("turns it off when the change is not the launch's: a new chat that never came swallows no later switch", () => {
    // The launch left c-1, but the next change opens another conversation.
    expect(keepsInvestigationMode({ left: "c-1", next: "c-2", launchLeft: "c-1" })).toBe(false);
    // Or leaves another conversation than the one the launch left.
    expect(keepsInvestigationMode({ left: "c-3", next: null, launchLeft: "c-1" })).toBe(false);
  });

  it("a launch that left no conversation keeps nothing: there is no change of its own to skip", () => {
    expect(keepsInvestigationMode({ left: null, next: null, launchLeft: null })).toBe(false);
  });
});
