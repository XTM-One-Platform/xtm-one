// Only the conversation the new chat's draft just created keeps the draft's choices
// (#11462): a conversation created on the page, then opened again after a new chat,
// reads its own saved tools.

import { describe, expect, it } from "vitest";

import { createdFromDraft } from "@/pages/chat-panel/created-from-draft";

describe("createdFromDraft", () => {
  it("names the conversation a draft's first send created", () => {
    expect(createdFromDraft("c-1", { created: true })).toBe("c-1");
  });

  it("names none for a conversation opened, also one created earlier on the page", () => {
    expect(createdFromDraft("c-1", { created: false })).toBeNull();
  });

  it("names none for a new chat", () => {
    expect(createdFromDraft(null, { created: false })).toBeNull();
  });
});
