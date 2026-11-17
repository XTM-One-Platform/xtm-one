import { afterEach, describe, expect, it } from "vitest";

import { chatDraftStorageKey } from "@/lib/chat-launch";
import { CHAT_AUTOSEND_STORAGE_KEY, CHAT_PENDING_AGENT_STORAGE_KEY } from "@/lib/constants";
import { organizationOnboardingPrompt, primeOrganizationOnboarding } from "@/lib/organization-onboarding";

const identity = (text: string) => text;

describe("organization onboarding (#11224)", () => {
  afterEach(() => sessionStorage.clear());

  it("asks the assistant to interview one field at a time until every field is filled or skipped", () => {
    const prompt = organizationOnboardingPrompt(identity);
    expect(prompt).toContain("one question at a time");
    expect(prompt).toContain("filled or skipped");
  });

  it("primes the next new chat with the default assistant and sends it on arrival", () => {
    primeOrganizationOnboarding(identity);
    expect(sessionStorage.getItem(chatDraftStorageKey(null))).toBe(organizationOnboardingPrompt(identity));
    // No agent named: /chat resolves the user's default agent.
    expect(sessionStorage.getItem(CHAT_PENDING_AGENT_STORAGE_KEY)).toBeNull();
    expect(sessionStorage.getItem(CHAT_AUTOSEND_STORAGE_KEY)).toBe("1");
  });
});
