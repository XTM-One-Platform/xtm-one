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

import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Alert } from "@filigran/design-system";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { useDismissBuiltinTools, useUpdateAgent } from "@/hooks/api/useAgentApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import {
  ADDED_TOOLS_PARAM,
  builtinAllowedPayload,
  dismissedBuiltinFunctions,
  pendingAddedFunctions,
  withFunctionsAdded,
} from "@/lib/builtin-allowed-tools";
import type { Agent } from "@/types";

type PendingFunction = { groupId: string; name: string };

const qualified = (fn: PendingFunction) => `${fn.groupId}.${fn.name}`;

/**
 * The built-in functions a release shipped that this agent's allowlist leaves
 * out, named by the owner's release notice (`?added=group.fn,...`, #12192).
 * Nothing is granted by itself: a viewer who may edit the agent adds one
 * function, or all of them, in one click (AI-gated: saving the agent is an AI
 * route). A function added since, or a group no longer scoped, is not listed.
 * An owner who leaves one out on purpose dismisses it, or all of them
 * (#12245): the names are kept on the agent server side, never listed again
 * here, and the bell's release notice is marked read once nothing it names is
 * left to act on. The group's function picker still offers them.
 */
export function NewBuiltinToolsNotice({
  agent,
  canManage = false,
  groupFunctions,
}: {
  agent: Pick<Agent, "id" | "builtin_tool_ids" | "builtin_allowed_tools"> & Partial<Pick<Agent, "config">>;
  canManage?: boolean;
  /** The functions a built-in group grants on this deployment. */
  groupFunctions: (groupId: string) => readonly string[];
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const updateAgent = useUpdateAgent();
  const dismissTools = useDismissBuiltinTools();
  const aiOff = useAiActionDisabled();
  const [searchParams] = useSearchParams();
  // Dismissed in this session, before the agent is read again.
  const [dismissedNow, setDismissedNow] = useState<string[]>([]);
  const dismissed = [...dismissedBuiltinFunctions(agent.config), ...dismissedNow];
  const pending = pendingAddedFunctions(searchParams.get(ADDED_TOOLS_PARAM), agent.builtin_allowed_tools, groupFunctions, dismissed);
  if (pending.length === 0) return null;
  const busy = updateAgent.isPending || dismissTools.isPending || aiOff;

  const add = (functions: PendingFunction[]) =>
    updateAgent.mutate(
      {
        id: agent.id,
        builtin_allowed_tools: builtinAllowedPayload(
          withFunctionsAdded(agent.builtin_allowed_tools, functions),
          agent.builtin_tool_ids ?? [],
        ),
      },
      {
        onSuccess: () => {
          toast({
            title: t_i18n("{count, plural, one {# tool added to this agent} other {# tools added to this agent}}", { count: functions.length }),
            variant: "success",
          });
          // Nothing dismissed: marks the bell's notice read once nothing it names is left.
          dismissTools.mutate({ id: agent.id, dismiss: [] });
        },
      },
    );

  const dismiss = (functions: PendingFunction[]) => {
    const names = functions.map(qualified);
    dismissTools.mutate(
      { id: agent.id, dismiss: names },
      {
        onSuccess: () => {
          setDismissedNow((current) => [...current, ...names]);
          toast({
            title: t_i18n("{count, plural, one {# tool dismissed: add it later from its group} other {# tools dismissed: add them later from their group}}", { count: names.length }),
            variant: "success",
          });
        },
      },
    );
  };

  // The DS Alert (UX round 1 B1, #12213). Add all sits on the title row, its
  // right edge on the same column as each row's Add (B2): both end at the
  // Alert's content edge, so the trailing action slot is not used.
  return (
    <Alert
      severity="info"
      className="mb-3"
      data-testid="new-builtin-tools-notice"
      title={
        <span className="flex w-full items-center justify-between gap-3">
          <span className="min-w-0">
            {t_i18n("{count, plural, one {# new built-in tool is outside this agent's allowlist} other {# new built-in tools are outside this agent's allowlist}}", { count: pending.length })}
          </span>
          {canManage && pending.length > 1 && (
            <span className="flex shrink-0 items-center gap-2">
              <AiDisabledTip>
                <Button variant="ghost" size="sm" disabled={busy} onClick={() => dismiss(pending)} data-testid="new-builtin-tools-dismiss-all">
                  {t_i18n("Dismiss all")}
                </Button>
              </AiDisabledTip>
              <AiDisabledTip>
                <Button variant="outline" size="sm" disabled={busy} onClick={() => add(pending)} data-testid="new-builtin-tools-add-all">
                  {t_i18n("Add all")}
                </Button>
              </AiDisabledTip>
            </span>
          )}
        </span>
      }
      description={
        <span className="block space-y-2">
          <span className="block">
            {t_i18n("A release added them to a group this agent limits to some functions, so the agent does not get them. Add the ones it needs, dismiss the others.")}
          </span>
          <span role="list" className="block space-y-1">
            {pending.map((fn) => (
              <span role="listitem" key={qualified(fn)} className="flex items-center justify-between gap-3" data-testid="new-builtin-tool">
                <code className="content-compact min-w-0 break-all">{qualified(fn)}</code>
                {canManage && (
                  <span className="flex shrink-0 items-center gap-2">
                    <AiDisabledTip>
                      <Button variant="ghost" size="sm" disabled={busy} onClick={() => dismiss([fn])} data-testid="new-builtin-tool-dismiss">
                        {t_i18n("Dismiss (built-in tool row)")}
                      </Button>
                    </AiDisabledTip>
                    <AiDisabledTip>
                      <Button variant="outline" size="sm" disabled={busy} onClick={() => add([fn])} data-testid="new-builtin-tool-add">
                        {t_i18n("Add")}
                      </Button>
                    </AiDisabledTip>
                  </span>
                )}
              </span>
            ))}
          </span>
        </span>
      }
    />
  );
}
