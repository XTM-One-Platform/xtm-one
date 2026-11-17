import { describe, expect, it } from "vitest";

import { agentModelChoice, parseModelChoice, ROUTING_MODEL_CHOICE } from "@/lib/utils";

describe("agentModelChoice", () => {
  it("is the routing choice for an agent on the platform routing model", () => {
    expect(
      agentModelChoice({ llm_model_source: "routing", llm_model: "gpt-5", llm_provider_id: "p1" }),
    ).toBe(ROUTING_MODEL_CHOICE);
  });

  it("is the agent's own model choice otherwise", () => {
    expect(agentModelChoice({ llm_model_source: null, llm_model: "gpt-5", llm_provider_id: "p1" })).toBe(
      "p1::gpt-5",
    );
    expect(agentModelChoice({ llm_model: "gpt-5" })).toBe("::gpt-5");
  });

  it("is empty (platform default) for an agent with no model and no source", () => {
    expect(agentModelChoice({ llm_model: null, llm_provider_id: null })).toBe("");
  });

  it("never decodes as a model id with a provider", () => {
    expect(parseModelChoice(ROUTING_MODEL_CHOICE).providerId).toBeNull();
  });
});
