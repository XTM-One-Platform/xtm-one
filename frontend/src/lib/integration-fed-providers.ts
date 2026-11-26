// SPDX-License-Identifier: Apache-2.0

/**
 * Platform LLM providers an integration can feed.
 *
 * An integration of these providers holds the account a platform LLM provider
 * runs on: its console creates that provider from the integration's key, or
 * adopts one made in Settings, then picks the models it offers (OpenRouter
 * today, `services/openrouter_llm_provider.py`; LM Studio, reached
 * on the integration's route, `services/lmstudio_llm_provider.py`). Settings reads this table to
 * send an admin adding such a provider to the integration rather than ask for
 * the key a second time. Pure, so `node --test` runs it
 * (`integration-fed-providers.test.ts`).
 */
import type { Integration, LLMProvider } from "@/types";

interface FeedingIntegration {
  /** The integration provider that feeds it. */
  integrationProvider: string;
  /** The integration detail tab that creates or feeds the provider. */
  tab: string;
  /** What the integration needs before it may feed (the server refuses any
   *  other: `link_refusal`), besides being company-managed. */
  ready: (integration: Integration) => boolean;
  /** The provider runs on its integration alone (`integration_fed_only`):
   *  Settings never adds one by hand, the server refuses it. */
  onlyFromIntegration?: boolean;
}

function hasServerUrl(integration: Integration): boolean {
  const url = integration.extra_config?.base_url;
  return typeof url === "string" && url.trim() !== "";
}

/** LLM provider key → the integration that feeds it. */
const INTEGRATION_FED_LLM_PROVIDERS: Readonly<Record<string, FeedingIntegration>> = {
  openrouter: { integrationProvider: "openrouter", tab: "models", ready: (i) => i.has_api_key },
  // LM Studio needs its server URL (reached directly or through a relay group), its token optional.
  lmstudio: { integrationProvider: "lmstudio", tab: "models", ready: hasServerUrl, onlyFromIntegration: true },
};

/** Whether Settings may add a `llmProvider` config by hand (false: only its integration creates one). */
export function addableByHand(llmProvider: string): boolean {
  return !INTEGRATION_FED_LLM_PROVIDERS[llmProvider]?.onlyFromIntegration;
}

/**
 * The integrations that could feed a new `llmProvider` config: of the
 * integration provider that feeds it, company-managed and ready to feed
 * (an API key for OpenRouter, a server URL for LM Studio), and not feeding a
 * config yet (an integration feeds one at most). Empty for an LLM provider no
 * integration feeds.
 */
export function integrationsToFeed(
  llmProvider: string,
  integrations: readonly Integration[],
  providers: readonly Pick<LLMProvider, "source_integration_id">[],
): Integration[] {
  const fed = INTEGRATION_FED_LLM_PROVIDERS[llmProvider];
  if (!fed) return [];
  const feeding = new Set(providers.map((p) => p.source_integration_id).filter(Boolean));
  return integrations.filter(
    (i) => i.provider === fed.integrationProvider && i.is_company_managed && fed.ready(i) && !feeding.has(i.id),
  );
}

/** The page of `integrationId` that creates or feeds its `llmProvider` config. */
export function feedingPagePath(integrationId: string, llmProvider: string): string | null {
  const fed = INTEGRATION_FED_LLM_PROVIDERS[llmProvider];
  return fed ? `/integrations/${integrationId}/${fed.tab}` : null;
}
