// SPDX-License-Identifier: Apache-2.0

import { Icon } from "@filigran/design-system";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";
import { feedingPagePath, integrationsToFeed } from "@/lib/integration-fed-providers";
import { PROVIDER_DISPLAY_NAMES } from "@/lib/constants";
import type { Integration, LLMProvider } from "@/types";

/**
 * In the Add Provider dialog: the company-managed integrations that could feed
 * the provider being added (OpenRouter). Its key is already stored there and its
 * Models tab creates the provider from it, so the admin is sent there rather
 * than asked for the key a second time. Renders nothing when none could.
 */
export function IntegrationFedProviderHint({
  llmProvider,
  integrations,
  providers,
}: {
  llmProvider: string;
  integrations: readonly Integration[];
  providers: readonly LLMProvider[];
}) {
  const { t_i18n } = useFormatter();
  const candidates = integrationsToFeed(llmProvider, integrations, providers);
  if (candidates.length === 0) return null;
  return (
    <div className="space-y-2 rounded-sm border border-accent/20 bg-accent/5 p-3">
      <p className="content-compact text-default-secondary">
        {t_i18n("A company-managed {provider} integration already holds the account's key. Create the provider from its Models tab: you pick the models from its catalog and its key follows the integration's.", {
          provider: PROVIDER_DISPLAY_NAMES[llmProvider] ?? llmProvider,
        })}
      </p>
      <div className="flex flex-wrap gap-2">
        {candidates.map((integration) => (
          <Button key={integration.id} asChild variant="secondary" size="sm" className="gap-1.5">
            <Link to={feedingPagePath(integration.id, llmProvider) ?? `/integrations/${integration.id}`}>
              <Icon name="link-2" size={16} />
              {t_i18n("Create from {name}", { name: integration.name })}
            </Link>
          </Button>
        ))}
      </div>
    </div>
  );
}
