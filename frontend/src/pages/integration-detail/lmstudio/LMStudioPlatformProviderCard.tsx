import { Link } from "react-router-dom";
import { Boxes, Server, Settings2 } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useAuth } from "@/hooks/useAuth";
import { useLMStudioOverview } from "@/hooks/api/useLMStudioApi";
import { useFormatter } from "@/i18n";
import type { Integration } from "@/types";

/**
 * On the integration's Overview: whether the platform LLM provider runs on this
 * LM Studio server, and the way to the Models tab that creates or feeds it.
 *
 * Reads the console overview the Models tab reads (same query). Renders nothing
 * while it loads, when it fails or with AI off (the console tabs say why), and
 * for a non-admin while no provider is fed, since only an admin can make one.
 */
export function LMStudioPlatformProviderCard({ integration }: { integration: Integration }) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const aiOff = useAiActionDisabled();
  const overview = useLMStudioOverview(integration.id, !aiOff);

  if (aiOff || !overview.data) return null;
  const { llm_providers, can_link_provider, link_refusal } = overview.data;
  const provider = llm_providers[0];
  if (!provider && !isAdmin) return null;
  const modelsPath = `/integrations/${integration.id}/models`;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Server className="h-4 w-4 text-accent" />
          {t_i18n("Platform LLM provider")}
        </CardTitle>
        <CardDescription>
          {t_i18n("The LM Studio provider every agent and the chat can run on, reached through this integration.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-wrap items-center justify-between gap-3 content-base">
        {provider ? (
          <>
            <div className="space-y-1">
              <p className="font-medium">{provider.display_name || "LM Studio"}</p>
              <p className="content-compact text-default-secondary">
                {t_i18n("{count, plural, one {# model offered} other {# models offered}}", { count: provider.available_models?.length ?? 0 })}
                {provider.default_model ? ` · ${t_i18n("Default: {model}", { model: provider.default_model })}` : ""}
              </p>
            </div>
            <Button asChild variant="secondary" size="sm" className="gap-1.5">
              <Link to={modelsPath}>
                <Boxes className="h-3.5 w-3.5" />
                {t_i18n("Pick the models")}
              </Link>
            </Button>
          </>
        ) : can_link_provider ? (
          <>
            <p className="content-compact text-default-secondary">
              {t_i18n("The platform does not run on this integration yet. Offer the models you want in the Models tab, then create the provider from them.")}
            </p>
            <Button asChild size="sm" className="gap-1.5">
              <Link to={modelsPath}>
                <Boxes className="h-3.5 w-3.5" />
                {t_i18n("Pick the models")}
              </Link>
            </Button>
          </>
        ) : (
          <>
            <p className="content-compact text-default-secondary">{link_refusal}</p>
            <Button asChild variant="secondary" size="sm" className="gap-1.5">
              <Link to={`/integrations/${integration.id}/config`}>
                <Settings2 className="h-3.5 w-3.5" />
                {t_i18n("Open Configuration")}
              </Link>
            </Button>
          </>
        )}
      </CardContent>
    </Card>
  );
}
