import { describe, expect, it } from "vitest";

import { modelSelectOptions } from "@/components/ui/model-select-options";
import type { AvailableModel, LLMProvider } from "@/types";

/**
 * The options every model picker is built from: one per
 * offering under its provider config's header, a field label naming both, and
 * a saved choice nobody offers any more kept as its own entry.
 */

const t_i18n = (id: string, values?: Record<string, string | number>) =>
  id.replace(/\{(\w+)\}/g, (_, key: string) => String(values?.[key] ?? ""));

const providers = [
  { id: "p-openai", provider: "openai", display_name: null },
  { id: "p-claude", provider: "anthropic", display_name: "Claude" },
] as unknown as LLMProvider[];

const models: AvailableModel[] = [
  { model: "gpt-4o", provider: "openai", provider_id: "p-openai" },
  { model: "claude-sonnet", provider: "anthropic", provider_id: "p-claude" },
];

describe("modelSelectOptions", () => {
  it("puts each offering under its config's name and names both in the field", () => {
    expect(modelSelectOptions(models, providers, "", t_i18n)).toEqual([
      {
        value: "p-openai::gpt-4o",
        label: "gpt-4o",
        selectedLabel: "gpt-4o (OpenAI)",
        group: "OpenAI",
        keywords: ["openai", "OpenAI"],
      },
      {
        value: "p-claude::claude-sonnet",
        label: "claude-sonnet",
        selectedLabel: "claude-sonnet (Claude)",
        group: "Claude",
        keywords: ["anthropic", "Anthropic"],
      },
    ]);
  });

  it("adds nothing when the value is one of the offerings", () => {
    expect(modelSelectOptions(models, providers, "p-claude::claude-sonnet", t_i18n)).toHaveLength(2);
  });

  it("keeps a saved model its config no longer offers, under that config", () => {
    const [saved] = modelSelectOptions(models, providers, "p-claude::claude-opus", t_i18n);
    expect(saved).toEqual({
      value: "p-claude::claude-opus",
      label: "claude-opus",
      selectedLabel: "claude-opus (Claude)",
      group: "Claude",
    });
  });

  it("says an older choice pins no provider instead of guessing one", () => {
    const [saved] = modelSelectOptions(models, providers, "gpt-4o", t_i18n);
    expect(saved).toEqual({ value: "gpt-4o", label: "gpt-4o: provider not pinned" });
  });
});
