/**
 * Settings says "No LLM provider configured" only when none is: a provider
 * that lists no model gets its own notice (#6) - `yarn test`.
 */
import { describe, expect, it } from "vitest";
import type { LLMProvider } from "@/types";
import { emptyChatProviders } from "./emptyChatProviders";

const provider = (over: Partial<LLMProvider>): LLMProvider =>
  ({ id: over.provider ?? "p", provider: "openai", capabilities: ["chat"], available_models: null, ...over }) as LLMProvider;

describe("emptyChatProviders", () => {
  it("is empty when no provider is configured", () => {
    expect(emptyChatProviders(undefined)).toEqual([]);
    expect(emptyChatProviders([])).toEqual([]);
  });

  it("names the chat providers when none of them lists a model", () => {
    const gateway = provider({ provider: "filigran_gateway", available_models: [] });
    const openai = provider({ provider: "openai" });
    expect(emptyChatProviders([gateway, openai])).toEqual([gateway, openai]);
  });

  it("is empty when a chat provider lists a model", () => {
    const listing = provider({ provider: "openai", available_models: ["gpt-5.4"] });
    expect(emptyChatProviders([provider({ provider: "filigran_gateway" }), listing])).toEqual([]);
  });

  it("leaves out the providers that do not chat", () => {
    const tei = provider({ provider: "tei", capabilities: ["embedding"] });
    expect(emptyChatProviders([tei])).toEqual([]);
  });
});
