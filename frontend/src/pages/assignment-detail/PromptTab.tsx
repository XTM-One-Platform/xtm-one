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
import { useProviderCatalog, useIntegrations } from "@/hooks/api/useIntegrationApi";
import { useUpdateAssignment } from "@/hooks/api/useAssignmentApi";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { normalizeScopeEnforcement } from "@/lib/element-scope";
import type { Assignment, Agent, IntegrationElement } from "@/types";
import { PromptCard } from "./PromptCard";
import { IntegrationBindingsCard } from "./IntegrationBindingsCard";
import { ElementBindingsCard } from "./ElementBindingsCard";
import { BuiltinScopeCard } from "./BuiltinScopeCard";
import { OutcomesCard, type OutcomesParent } from "@/components/roi/OutcomesCard";
import { hasStructuredOutput } from "@/components/roi/roi-outcomes";

// ---------------------------------------------------------------------------
// Prompt Tab — Action prompt + Integration bindings + Element bindings
// ---------------------------------------------------------------------------

interface PromptTabProps {
  assignment: Assignment;
  agent: Agent | undefined;
  canManage: boolean;
  onUpdate: () => void;
}

export function PromptTab({ assignment, agent, canManage, onUpdate }: PromptTabProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data: catalog } = useProviderCatalog("integration");
  const { data: allIntegrations } = useIntegrations({ scope: "all" });
  const update = useUpdateAssignment();

  // Element binding editing state
  const [editingElements, setEditingElements] = useState(false);
  const [elementBindingsDraft, setElementBindingsDraft] = useState<Record<string, IntegrationElement[]>>({});
  const [enforcementDraft, setEnforcementDraft] = useState<string[]>([]);

  // The assignment wins over its agent, the agent over its home fleet (design 3.2).
  const outcomeParents: OutcomesParent[] = [];
  if (agent) outcomeParents.push({ subject: { type: "agent", id: agent.id }, kind: "agent", name: agent.name });
  if (agent?.fleet) outcomeParents.push({ subject: { type: "fleet", id: agent.fleet.fleet_id }, kind: "fleet", name: agent.fleet.fleet_name });

  return (
    <div className="space-y-6">
      {/* Prompt section: gate prompt (standalone) + 50/50 grid (action prompt | integration bindings) */}
      <PromptCard
        assignment={assignment}
        canManage={canManage}
        onUpdate={onUpdate}
        renderRight={
          <IntegrationBindingsCard
            assignment={assignment}
            agent={agent}
            allIntegrations={allIntegrations}
            canManage={canManage}
            onUpdate={onUpdate}
          />
        }
      />

      {/* Integration Element Bindings (channels, repos, etc.) */}
      <ElementBindingsCard
        assignment={assignment}
        agent={agent}
        catalog={catalog}
        canManage={canManage}
        editing={editingElements}
        draft={elementBindingsDraft}
        enforcementDraft={enforcementDraft}
        onStartEditing={() => {
          setElementBindingsDraft(assignment.integration_element_bindings ?? {});
          setEnforcementDraft(assignment.integration_element_enforcement ?? []);
          setEditingElements(true);
        }}
        onCancel={() => setEditingElements(false)}
        onSave={(bindings) => {
          const payload = Object.keys(bindings).length > 0 ? bindings : null;
          update.mutate(
            {
              id: assignment.id,
              integration_element_bindings: payload,
              integration_element_enforcement: normalizeScopeEnforcement(enforcementDraft, payload),
            },
            {
              onSuccess: () => {
                toast({ title: t_i18n("Element bindings updated"), variant: "success" });
                setEditingElements(false);
                onUpdate();
              },
            },
          );
        }}
        onDraftChange={setElementBindingsDraft}
        onEnforcementDraftChange={setEnforcementDraft}
      />

      {/* Per-function scoping of the agent's built-in groups */}
      <BuiltinScopeCard assignment={assignment} agent={agent} canManage={canManage} onUpdate={onUpdate} />

      {/* The unit of work this assignment's runs count under in ROI (#9536) */}
      <OutcomesCard
        subject={{ type: "assignment", id: assignment.id }}
        parents={outcomeParents}
        structuredOutput={hasStructuredOutput(agent)}
        canManage={canManage}
        testId="assignment-roi-outcomes-card"
      />
    </div>
  );
}
