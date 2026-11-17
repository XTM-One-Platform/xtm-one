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
 * "From a connected integration" on the organization profile (#11888, slice C PR 2b):
 * the administrator picks a connected integration (Intune, an EDR, a cloud
 * inventory, a CMDB), optionally asks for a daily sync, and lands in a chat
 * with the fill agent POST /organization-context/fill-agent made for it. The
 * chat previews the rows first; nothing is written to the profile until the
 * administrator says so. The daily sync, when on, applies on its own.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Spinner } from "@filigran/design-system";

import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, SettingsSwitchField, settingsFieldLabelId } from "@/components/ui/settings-form";
import { useCreateOrganizationFillAgent } from "@/hooks/api/useOrganizationContextApi";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { primeChatDraft } from "@/lib/chat-launch";

const INTEGRATION_FIELD = "org-fill-integration";
const SYNC_FIELD = "org-fill-sync";
/** An integration an agent can read: its credential is set. */
const READABLE_STATUSES = new Set(["connected", "configured"]);

interface OrganizationFillAgentDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

function browserTimezone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
  } catch {
    return undefined;
  }
}

export function OrganizationFillAgentDialog({ open, onOpenChange }: OrganizationFillAgentDialogProps) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { data: integrations = [], isLoading } = useIntegrations();
  const create = useCreateOrganizationFillAgent();
  // The fill agent is an AI action: with AI off the button is disabled, not refused on click (FR-018).
  const aiOff = useAiActionDisabled();
  const [integrationId, setIntegrationId] = useState("");
  const [sync, setSync] = useState(false);
  const [failed, setFailed] = useState(false);

  const readable = integrations.filter((integration) => READABLE_STATUSES.has(integration.status));
  const options = readable.map((integration) => ({ value: integration.id, label: integration.name || integration.provider }));

  const start = () => {
    if (!integrationId || aiOff) return;
    setFailed(false);
    create.mutate(
      { integration_id: integrationId, schedule: sync, timezone: browserTimezone() },
      {
        onSuccess: (made) => {
          // The administrator reads the preview instruction and sends it: nothing runs before that.
          primeChatDraft(made.prompt, { agentId: made.agent_id });
          onOpenChange(false);
          navigate("/chat");
        },
        onError: () => setFailed(true),
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{t_i18n("Fill from a connected integration")}</DialogTitle>
          <DialogDescription>
            {t_i18n("An agent reads the integration and previews the assets in a chat; nothing is saved until you confirm.")}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="-m-1 space-y-4 p-1">
          <SettingsField
            id={INTEGRATION_FIELD}
            label={t_i18n("Integration")}
            required
            helper={
              !isLoading && readable.length === 0
                ? t_i18n("No connected integration yet: connect one in Settings > Integrations.")
                : t_i18n("The agent gets read-only access to it.")
            }
          >
            <SearchableSelect
              id={INTEGRATION_FIELD}
              aria-labelledby={settingsFieldLabelId(INTEGRATION_FIELD)}
              options={options}
              value={integrationId}
              onValueChange={setIntegrationId}
              placeholder={t_i18n("Pick an integration")}
              disabled={isLoading || readable.length === 0}
            />
          </SettingsField>
          <SettingsSwitchField
            id={SYNC_FIELD}
            label={t_i18n("Keep the assets in sync every day")}
            helper={t_i18n("The agent applies the changes every day at 06:00, rows written by hand kept.")}
            info={t_i18n(
              "On: a scheduled assignment runs the agent every day at 06:00 and applies the changes, rows written by hand kept. Off: only the chat, where you confirm each change. You can turn it off later in the agent's assignments.",
            )}
            checked={sync}
            onCheckedChange={setSync}
          />
          {failed && (
            <p role="alert" className="content-compact text-feedback-error-primary">
              {t_i18n("The agent could not be set up. Try again.")}
            </p>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            {t_i18n("Cancel")}
          </Button>
          <AiDisabledTip>
            <Button onClick={start} disabled={!integrationId || create.isPending || aiOff} aria-busy={create.isPending || undefined}>
              {create.isPending && <Spinner size="sm" tone="inherit" />}
              {t_i18n("Open the chat")}
            </Button>
          </AiDisabledTip>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
