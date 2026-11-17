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
 * Deleting an agent, and who continues its conversations.
 *
 * A deleted agent's conversations are kept with their whole history. Naming
 * a successor hands them to it - for the owners who can use it - so the next
 * message in each is answered by the successor, which reads the deleted
 * agent's answers as that agent's. Without one, every owner continues each
 * conversation with an agent of theirs from its menu. The agents offered are
 * the caller's chat agents, enabled, other than the one being deleted: what
 * `DELETE /agents/{id}?successor_agent_id=` accepts.
 */
import { useMemo, useState } from "react";
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { agentSelectOption } from "@/components/ui/entity-select-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useFormatter } from "@/i18n";
import type { Agent } from "@/types";

/** Picker value for "no successor". */
const NO_SUCCESSOR = "__none__";

export function AgentSuccessorDialog({
  agent,
  open,
  onOpenChange,
  confirmLabel,
  onConfirm,
}: {
  agent: Agent;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** "Delete" when this is the last step, "Continue" when a confirmation follows. */
  confirmLabel: string;
  /** The successor picked, `null` for none. */
  onConfirm: (successorAgentId: string | null) => void;
}) {
  const { t_i18n } = useFormatter();
  const { data: agents } = useAgents();
  const [picked, setPicked] = useState<string>(NO_SUCCESSOR);

  const options = useMemo(
    () =>
      (agents ?? [])
        .filter((a) => a.id !== agent.id && a.enabled && !a.disable_chat)
        .map((a) => agentSelectOption(a, { description: a.is_company_managed ? t_i18n("Company") : undefined })),
    [agent.id, agents, t_i18n],
  );

  const close = (next: boolean) => {
    if (!next) setPicked(NO_SUCCESSOR);
    onOpenChange(next);
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-feedback-error-primary/10">
              <Icon name="triangle-alert" size={20} className="text-feedback-error-primary" />
            </div>
            <div className="space-y-1">
              <DialogTitle className="text-base">{t_i18n("Delete agent \"{name}\"?", { name: agent.name })}</DialogTitle>
              <DialogDescription className="content-base leading-relaxed">
                {t_i18n(
                  "This permanently deletes the agent and all its assignments. Its conversations are kept with their whole history.",
                )}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>
        <div className="space-y-1.5">
          <span id="agent-successor-label" className="content-compact font-medium text-default-primary">
            {t_i18n("Continue its conversations with")}
          </span>
          <SearchableSelect
            aria-labelledby="agent-successor-label"
            options={options}
            pinned={[{ value: NO_SUCCESSOR, label: t_i18n("No agent") }]}
            value={picked}
            onValueChange={(v) => setPicked(v || NO_SUCCESSOR)}
            placeholder={t_i18n("Search agents...")}
            emptyMessage={t_i18n("No agent to continue with")}
          />
          <p className="content-caption text-default-secondary">
            {picked === NO_SUCCESSOR
              ? t_i18n("Each owner can continue a conversation with an agent of theirs from its menu.")
              : t_i18n(
                  "The conversations of the people who can use it continue with it, and it reads this agent's earlier answers.",
                )}
          </p>
        </div>
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" size="sm" onClick={() => close(false)}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            size="sm"
            className="border-0 bg-feedback-error-primary text-negative-primary hover:bg-feedback-error-primary/90"
            onClick={() => {
              onConfirm(picked === NO_SUCCESSOR ? null : picked);
              close(false);
            }}
          >
            {confirmLabel}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
