/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { useUpdateAgent } from "@/hooks/api/useAgentApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { bindingsWithout } from "@/hooks/useCredentialSharingConfirm";
import { useFormatter } from "@/i18n";
import type { Agent } from "@/types";

/**
 * The integrations this agent binds that its owner can no longer see (a share
 * revoked, the owner removed from the group): its runs refuse their tools
 * until the owner's access comes back or the binding is removed (#11229).
 * Each integration links to its page; a viewer who may edit the agent removes
 * the binding here, in one click (AI-gated: saving the agent is an AI route).
 */
export function OrphanedIntegrationsNotice({
  agent,
  canManage = false,
}: {
  agent: Pick<Agent, "id" | "orphaned_integrations" | "integration_bindings">;
  canManage?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const updateAgent = useUpdateAgent();
  const aiOff = useAiActionDisabled();
  const orphaned = agent.orphaned_integrations ?? [];
  if (orphaned.length === 0) return null;

  const removeBinding = (integration: { id: string; name: string }) =>
    updateAgent.mutate(
      { id: agent.id, integration_bindings: bindingsWithout(agent, integration.id) },
      {
        onSuccess: () =>
          toast({ title: t_i18n("{name} is no longer bound to this agent", { name: integration.name }), variant: "success" }),
      },
    );

  return (
    <div
      role="status"
      className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.06] p-3"
      data-testid="orphaned-integrations-notice"
    >
      <div className="flex items-start gap-2">
        <Icon name="triangle-alert" size={16} className="text-feedback-warning-primary mt-0.5 shrink-0" />
        <div className="min-w-0 flex-1 space-y-2">
          <div className="space-y-1">
            <p className="content-base font-medium">
              {t_i18n("{count, plural, one {Owner no longer has access to this integration} other {Owner no longer has access to these integrations}}", { count: orphaned.length })}
            </p>
            <p className="content-compact text-default-secondary">
              {t_i18n("{count, plural, one {Its runs refuse this integration's tools until its owner gets access back or the binding is removed.} other {Its runs refuse the tools of these # integrations until its owner gets access back or the bindings are removed.}}", { count: orphaned.length })}
            </p>
          </div>
          <ul className="space-y-1">
            {orphaned.map((integration) => (
              <li key={integration.id} className="flex items-center justify-between gap-3" data-testid="orphaned-integration">
                <Link
                  to={`/integrations/${integration.id}`}
                  className="content-base font-medium truncate text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus rounded-sm"
                >
                  {integration.name}
                </Link>
                {canManage && (
                  <AiDisabledTip>
                    <Button
                      variant="outline"
                      disabled={updateAgent.isPending || aiOff}
                      onClick={() => removeBinding(integration)}
                    >
                      {t_i18n("Remove binding")}
                    </Button>
                  </AiDisabledTip>
                )}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
