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

import { Icon } from "@filigran/design-system";
import { useNavigate } from "react-router-dom";

import { DashboardNotice, NoticeAction } from "@/components/ui/dashboard-notice";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { useGeneralAssistantConfig } from "@/hooks/api/useSettingsApi";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";

/**
 * "No default model" / "no embedding provider" — the two configuration steps
 * an Enterprise deployment needs before its agents can run.
 *
 * Silent when AI is not licensed: on Community Edition these warnings walked
 * an administrator through configuring providers that the edition cannot
 * use, then left them fully blocked. The `EnterpriseUpsellNotice` above them
 * in `DashboardNotices` is the one thing to say there; these come back the
 * moment Enterprise Edition is active, which is when they become actionable.
 */
export function ModelConfigWarnings() {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const navigate = useNavigate();
  const licensed = useAgenticAIEnabled();
  // Skip the fetch too, not just the render: nothing here is actionable yet.
  const { data: gaConfig } = useGeneralAssistantConfig({ enabled: licensed });

  if (!licensed) return null;

  const missingDefaultModel = gaConfig !== undefined && !gaConfig.agent_default_model;
  const missingEmbeddingProvider = gaConfig !== undefined && !gaConfig.embedding_provider;

  if (!missingDefaultModel && !missingEmbeddingProvider) {
    return null;
  }

  const configureAction = isAdmin ? (
    <NoticeAction variant="warning" onClick={() => navigate("/settings/ai/models")}>
      {t_i18n("Configure")} <Icon name="arrow-right" size={16} />
    </NoticeAction>
  ) : undefined;

  return (
    <div className="space-y-3">
      {missingDefaultModel && (
        <DashboardNotice variant="warning" title={t_i18n("No default model configured")} actions={configureAction}>
          {isAdmin
            ? t_i18n("XTM One cannot perform any agent or model operation until a default model is set.")
            : t_i18n("XTM One cannot perform any agent or model operation until a default model is set. Contact your administrator to configure it.")}
        </DashboardNotice>
      )}

      {missingEmbeddingProvider && (
        <DashboardNotice variant="warning" title={t_i18n("No embedding provider configured")} actions={configureAction}>
          {isAdmin
            ? t_i18n("Knowledge Bases and any agent relying on embeddings will be inoperable.")
            : t_i18n("Knowledge Bases and any agent relying on embeddings will be inoperable. Contact your administrator to configure it.")}
        </DashboardNotice>
      )}
    </div>
  );
}
