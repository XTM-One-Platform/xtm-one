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

import { Hash, Info } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CountBadge } from "@/components/ui/count-badge";
import { INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { hasScopeSelection, isElementScopeEnforceable, isScopeEnforced, withScopeEnforcement } from "@/lib/element-scope";
import { ElementTypeEditor } from "@/components/ui/element-type-editor";
import { ScopeEnforcementBadge, ScopeEnforcementSwitch } from "@/components/ui/scope-enforcement-badge";
import { useFormatter } from "@/i18n";
import { CardEditActions } from "@/components/ui/card-edit-actions";
import type { Assignment, Agent, ProviderCatalogItem, IntegrationElement } from "@/types";

// ---------------------------------------------------------------------------
// Element Bindings Card — select channels, repos, etc. for this assignment
// Assignments refine from agent-level element selections.
// ---------------------------------------------------------------------------
export function ElementBindingsCard({
  assignment, agent, catalog, canManage,
  editing, draft, enforcementDraft, onStartEditing, onCancel, onSave, onDraftChange, onEnforcementDraftChange,
}: {
  assignment: Assignment;
  agent: Agent | undefined;
  catalog: ProviderCatalogItem[] | undefined;
  canManage: boolean;
  editing: boolean;
  draft: Record<string, IntegrationElement[]>;
  /** Element-binding keys switched to "Enforce as a boundary" in the draft. */
  enforcementDraft: string[];
  onStartEditing: () => void;
  onCancel: () => void;
  onSave: (bindings: Record<string, IntegrationElement[]>) => void;
  onDraftChange: (bindings: Record<string, IntegrationElement[]>) => void;
  onEnforcementDraftChange: (enforcement: string[]) => void;
}) {
  const { t_i18n } = useFormatter();
  // Determine effective bindings (assignment-level or inherited agent-level)
  const effectiveBindings = assignment.integration_bindings ?? agent?.integration_bindings ?? {};
  const boundProviders = Object.keys(effectiveBindings);

  // Find which providers have element types
  const providersWithElements = (catalog ?? []).filter(
    (p) => p.element_types.length > 0 && boundProviders.includes(p.provider),
  );

  // Nothing to show if no bound integrations support element types
  if (providersWithElements.length === 0) return null;

  const currentBindings = assignment.integration_element_bindings ?? {};
  const agentBindings = agent?.integration_element_bindings ?? {};
  const totalBound = Object.values(currentBindings).reduce((s, items) => s + items.length, 0);
  const agentTotalBound = Object.values(agentBindings).reduce((s, items) => s + items.length, 0);
  // The stricter of the agent and the assignment wins: a type the agent
  // enforces is a boundary for this assignment whatever its own switch says.
  const agentEnforces = (key: string) => isScopeEnforced(key, agentBindings, agent?.integration_element_enforcement);

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-2">
        <CardTitle className="content-base flex items-center gap-2">
          <Hash className="h-4 w-4 text-filigran-ia-primary" /> {t_i18n("Scope(s)")}
          {totalBound > 0 && <CountBadge variant="secondary">{totalBound}</CountBadge>}
        </CardTitle>
        <CardEditActions
          editing={editing}
          canEdit={canManage}
          aiGated
          onEdit={onStartEditing}
          onCancel={onCancel}
          onSave={() => onSave(draft)}
        />
      </CardHeader>
      <CardContent className="px-4 pb-4 pt-2">
        {/* Show agent-level elements as context */}
        {agentTotalBound > 0 && !editing && totalBound === 0 && (
          <div className="rounded-sm bg-filigran-ia-primary/5 border border-filigran-ia-primary/15 px-3 py-2 mb-2 flex items-start gap-2">
            <Info className="h-3.5 w-3.5 text-filigran-ia-primary mt-0.5 shrink-0" />
            <p className="content-caption text-filigran-ia-primary/80">
              {t_i18n("Inheriting {count, plural, one {# element} other {# elements}} from agent. Edit to select a subset for this assignment.", { count: agentTotalBound })}
            </p>
          </div>
        )}
        {editing ? (
          <div className="space-y-3">
            <p className="content-caption text-default-secondary">
              {agentTotalBound > 0
                ? t_i18n("Refine the agent's element selection for this assignment. Only elements selected at the agent level are available.")
                : t_i18n("Select specific channels, repositories, or other elements for this assignment to focus on.")}
            </p>
            {providersWithElements.map((provider) =>
              provider.element_types.map((et) => {
                const integrationId = (effectiveBindings[provider.provider] || [])[0];
                if (!integrationId) return null;
                const key = `${provider.provider}:${et.type}`;
                const agentScopedElements = agentBindings[key];
                const ownEnforced = isScopeEnforced(key, draft, enforcementDraft);
                const byAgent = agentEnforces(key);
                return (
                  <div key={key} className="space-y-1">
                    <div className="flex items-center justify-between gap-2">
                      <ScopeEnforcementBadge enforced={ownEnforced || byAgent} byAgent={byAgent && !ownEnforced} />
                      {isElementScopeEnforceable(key) && (
                        <ScopeEnforcementSwitch
                          checked={enforcementDraft.includes(key)}
                          hasSelection={hasScopeSelection(draft, key)}
                          lockedByAgent={byAgent}
                          onCheckedChange={(enforced) => onEnforcementDraftChange(withScopeEnforcement(enforcementDraft, key, enforced))}
                        />
                      )}
                    </div>
                    <ElementTypeEditor
                      integrationId={integrationId}
                      provider={provider.provider}
                      elementType={et}
                      selected={draft[key] ?? []}
                      allowedElements={agentScopedElements}
                      onSelectionChange={(items) => {
                        const next = { ...draft };
                        if (items.length > 0) {
                          next[key] = items;
                        } else {
                          delete next[key];
                        }
                        onDraftChange(next);
                      }}
                    />
                  </div>
                );
              }),
            )}
          </div>
        ) : (
          <div className="space-y-2">
            {totalBound === 0 && agentTotalBound === 0 ? (
              <p className="content-compact text-default-secondary italic py-3 text-center">
                {t_i18n("No target elements selected: assignment will operate on all available elements.")}
              </p>
            ) : totalBound === 0 ? null : (
              Object.entries(currentBindings).map(([key, items]) => {
                const colonIdx = key.indexOf(":");
                const providerKey = colonIdx >= 0 ? key.slice(0, colonIdx) : key;
                const etype = colonIdx >= 0 ? key.slice(colonIdx + 1) : key;
                const providerName = INTEGRATION_PROVIDER_NAMES[providerKey] ?? providerKey;
                const etLabel = (catalog ?? [])
                  .find((p) => p.provider === providerKey)
                  ?.element_types.find((et) => et.type === etype)?.name ?? etype;
                const ownEnforced = isScopeEnforced(key, currentBindings, assignment.integration_element_enforcement);
                const byAgent = agentEnforces(key);
                return (
                  <div key={key} className="rounded-sm bg-elevation-hover p-3">
                    <div className="mb-1.5 flex items-center gap-2">
                      <p className="content-caption text-default-secondary">{providerName} — {etLabel}</p>
                      <ScopeEnforcementBadge enforced={ownEnforced || byAgent} byAgent={byAgent && !ownEnforced} />
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {items.map((item) => (
                        <span key={item.id} className="content-compact bg-filigran-ia-primary/10 text-filigran-ia-primary rounded-sm px-2 py-0.5">
                          {item.name}{item.group ? ` (${item.group})` : ""}
                        </span>
                      ))}
                    </div>
                  </div>
                );
              })
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
