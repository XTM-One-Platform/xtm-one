import { describe, expect, it } from "vitest";

import type { LLMProvider } from "@/types";
import { decisionListingNotes } from "./decision-listing";

describe("the decision listing notes", () => {
  it("say why a listing provider offered none, unless it declares none", () => {
    const providers = ["listed", "empty", "failed", "unavailable", "openrouter"].map(
      (id) => ({ id, provider: "ollama", capabilities: ["chat", "decision"] }) as LLMProvider,
    );
    const notes = decisionListingNotes(providers, {
      models: [{ model: "clef-flash", provider: "ollama", provider_id: "listed" }],
      listings: [
        { provider_id: "listed", status: "listed" },
        { provider_id: "empty", status: "empty" },
        { provider_id: "failed", status: "failed" },
        { provider_id: "unavailable", status: "unavailable" },
      ],
    });
    expect(notes.map((n) => [n.provider.id, n.note])).toEqual([
      ["failed", "Listing the decision models of {provider} failed: the server log has the cause."],
      ["unavailable", "{provider} is not available to you, so it is not asked for its decision models."],
    ]);
  });
});
