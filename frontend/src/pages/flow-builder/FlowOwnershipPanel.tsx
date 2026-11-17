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

/**
 * Owner and access.
 *
 * The admin-only drawer that transfers a flow to another active user or makes
 * it company-managed - the same two controls an agent carries in its
 * Configuration > General, through the same components. Each change saves at
 * once through `PUT /flows/{id}` (`user_id` / `is_company_managed`); neither
 * touches the graph, so an unsaved canvas edit is left as it is.
 */

import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { OwnerCard } from "@/components/ui/owner-card";
import { CompanyManagedSwitch } from "@/components/ui/company-managed-switch";
import { useFormatter } from "@/i18n";
import type { Flow } from "@/types";

interface FlowOwnershipPanelProps {
  flow: Flow;
  /** True while an owner or company-managed change is saving. */
  isSaving: boolean;
  onTransferOwner: (userId: string) => void;
  onCompanyManagedChange: (companyManaged: boolean) => void;
  onClose: () => void;
}

export function FlowOwnershipPanel({
  flow,
  isSaving,
  onTransferOwner,
  onCompanyManagedChange,
  onClose,
}: FlowOwnershipPanelProps) {
  const { t_i18n } = useFormatter();

  return (
    <aside
      className="flex w-72 shrink-0 flex-col border-l border-elevation-subtle layer-1 bg-elevation-default"
      aria-label={t_i18n("Owner and access")}
    >
      <div className="flex items-center gap-2 border-b border-elevation-subtle p-3">
        <p className="flex-1 content-base font-medium">{t_i18n("Owner and access")}</p>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label={t_i18n("Close")}>
          <Icon name="x" size={16} />
        </Button>
      </div>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        <OwnerCard
          userId={flow.user_id}
          ownerName={flow.owner_name}
          onTransfer={onTransferOwner}
          disabled={isSaving}
          entityLabel="flow"
        />
        <CompanyManagedSwitch
          label={t_i18n("Company-managed flow")}
          description={
            flow.is_company_managed
              ? t_i18n("Shared with all users. Only admins can edit. Toggling off makes it private to its owner again.")
              : t_i18n("Private to its owner and the people it is shared with. Toggle on to share it with all users.")
          }
          checked={flow.is_company_managed}
          onCheckedChange={onCompanyManagedChange}
          disabled={isSaving}
        />
      </div>
    </aside>
  );
}
