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
 * Continue a conversation with another agent, in place.
 *
 * Picking another agent in the sidebar starts a NEW conversation: right for a
 * new subject, wrong when the agent that held this one was deleted, or the
 * person wants another agent to apply what they asked earlier. This dialog
 * rebinds the conversation instead (`PUT /chat/conversations/{id}/agent`):
 * the whole history stays, the chosen agent answers from the next message and
 * reads the earlier turns as the previous agent's, and a note "Continued by
 * <agent>" marks the change in the transcript.
 *
 * The agents offered are the ones a conversation could be started with here:
 * enabled, made for chat, and not the one holding it now. Once saved, the
 * panel showing the conversation reads it back: its new agent and the note.
 */
import { useMemo, useState, type RefObject } from "react";
import type { ChatPanelHandle } from "@filigran/chatbot";
import { Icon, Spinner } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { agentSelectOption } from "@/components/ui/entity-select-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useContinueConversationWithAgent } from "@/hooks/api/useChatApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import type { Agent } from "@/types";

/** Picker value for the General Assistant. */
const GENERAL_ASSISTANT = "__general__";

export function ContinueWithAgentDialog({
  conversationId,
  currentAgentId,
  deletedAgentName,
  candidates,
  offerGeneralAssistant = true,
  open,
  onClose,
  panel,
}: {
  conversationId: string | null;
  /** The agent holding the conversation now; `null`: the General Assistant, or nobody. */
  currentAgentId: string | null;
  /** The last name of the agent a deletion took from it, when one did. */
  deletedAgentName?: string | null;
  /** The agents to choose from (a studio's: those carrying its tool); every chat agent by default. */
  candidates?: readonly Agent[];
  offerGeneralAssistant?: boolean;
  open: boolean;
  onClose: () => void;
  /** The panel showing the conversation, which reads it back once the agent is saved. */
  panel: RefObject<ChatPanelHandle | null>;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  // The route sits behind the chat gate: with AI off it would only refuse.
  const aiOff = useAiActionDisabled();
  const { data: allAgents } = useAgents();
  const agents = candidates ?? allAgents;
  const continueWith = useContinueConversationWithAgent();
  const [picked, setPicked] = useState<string | null>(null);

  const options = useMemo(
    () =>
      (agents ?? [])
        .filter((a) => a.enabled && !a.disable_chat && a.id !== currentAgentId)
        .map((a) => agentSelectOption(a, { description: a.is_company_managed ? t_i18n("Company") : undefined })),
    [agents, currentAgentId, t_i18n],
  );
  const pinned = currentAgentId && offerGeneralAssistant
    ? [
        {
          value: GENERAL_ASSISTANT,
          label: t_i18n("General Assistant"),
          icon: <Icon name="sparkles" size={16} className="text-accent" />,
        },
      ]
    : [];

  const close = () => {
    setPicked(null);
    onClose();
  };

  const submit = () => {
    if (!conversationId || !picked) return;
    const agentId = picked === GENERAL_ASSISTANT ? null : picked;
    const name =
      agentId === null ? t_i18n("General Assistant") : (agents?.find((a) => a.id === agentId)?.name ?? t_i18n("Agent"));
    continueWith.mutate(
      { conversationId, agentId },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Continued with {agent}", { agent: name }), variant: "success" });
          panel.current?.reloadConversation();
          close();
        },
        onError: (err) => {
          toast({
            title: t_i18n("The conversation could not be continued with this agent"),
            description: err.message,
            variant: "error",
          });
        },
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t_i18n("Continue with another agent")}</DialogTitle>
          <DialogDescription>
            {t_i18n(
              "The conversation keeps its whole history: the agent you choose answers from your next message and reads the earlier answers as the previous agent's.",
            )}
          </DialogDescription>
        </DialogHeader>
        {deletedAgentName && (
          <p className="content-compact text-default-secondary">
            {t_i18n("Its agent {agent} was deleted.", { agent: deletedAgentName })}
          </p>
        )}
        <SearchableSelect
          aria-label={t_i18n("Agent")}
          options={options}
          pinned={pinned}
          value={picked}
          onValueChange={setPicked}
          placeholder={t_i18n("Search agents...")}
          emptyMessage={t_i18n("No agent to continue with")}
        />
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="outline" size="sm" onClick={close}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            size="sm"
            onClick={submit}
            disabled={aiOff || !picked || !conversationId || continueWith.isPending}
            aria-busy={continueWith.isPending}
          >
            {continueWith.isPending && <Spinner size="sm" tone="inherit" />}
            {t_i18n("Continue")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
