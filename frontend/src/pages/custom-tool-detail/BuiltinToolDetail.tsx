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

import { useNavigate } from "react-router-dom";
import { useTabParam } from "@/hooks/useTabParam";
import { Wrench, Sparkles, Bot, ArrowRight, Settings2, BarChart3, Activity, Building2 } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { Button } from "@/components/ui/button";
import { BackButton } from "@/components/ui/back-button";
import { PageSkeleton } from "@/components/ui/skeleton";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { StatCard } from "@/components/ui/stat-card";
import { EntityActivityTab } from "@/components/ui/entity-activity-tab";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { CompanyManagedBanner } from "@/components/ui/company-managed-banner";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { useAuth } from "@/hooks/useAuth";
import { useBuiltinTools } from "@/hooks/useBuiltinTools";
import { useAgents } from "@/hooks/api/useAgentApi";
import {
  useGeneralAssistantConfig, useUpdateGeneralAssistantConfig, useAvailableImageModels, useMediaModelsInUse,
} from "@/hooks/api/useSettingsApi";
import { useBoundAgentRunStats } from "@/hooks/api/useAssignmentApi";
import { ICON_MAP, PROVIDER_DISPLAY_NAMES } from "@/lib/constants";
import { formatCompactNumber, formatShortDate, formatTime, pluralize } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { ToolGateBadge } from "@/pages/tools/ToolGateBadge";
import { BuiltinToolsPending } from "@/pages/tools/BuiltinToolsPending";
import { useToolGateSummary } from "@/pages/tools/useToolGateSummary";
import { BuiltinToolsCard } from "./BuiltinToolsCard";

// ---------------------------------------------------------------------------
// Built-in Tool Detail Page
// ---------------------------------------------------------------------------

export function BuiltinToolDetail({ toolId }: { toolId: string }) {
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const { describe, carries, isReady: registryReady, isError: registryFailed } = useBuiltinTools();
  const { data: agents } = useAgents({ scope: "all" });
  const { data: gaConfig } = useGeneralAssistantConfig();
  const updateGAConfig = useUpdateGeneralAssistantConfig();
  const { data: availableImageModels } = useAvailableImageModels();
  const [tab] = useTabParam<"overview" | "config" | "activity">("overview");
  /* One page, several tool names — "Work Items" is six — so the header reports
     the aggregate, and the Configuration tab's "Available Tools" table answers
     per tool, beside the tool it is about. Read here, above the not-found
     return: a hook after an early return runs in a different order on the two
     paths. */
  const { byGroup } = useToolGateSummary();

  const tool = describe(toolId.replace(/^builtin-/, ""));
  const isImageTool = tool.id === "files" || tool.id === "generate_image";
  const { data: mediaModelsInUse } = useMediaModelsInUse(isImageTool && !!gaConfig && !gaConfig.image_model);
  // With none saved, the model images really run on (a built-in or AI Gateway default).
  const imageModelShown = gaConfig?.image_model || mediaModelsInUse?.image?.model || t_i18n("Auto (provider default)");
  // An agent still carrying a former id is bound to its group's page.
  const boundAgents = agents?.filter((a) => carries(a.builtin_tool_ids, tool.id)) || [];
  const boundAgentIds = new Set(boundAgents.map((a) => a.id));
  const { totalRuns, lastRun } = useBoundAgentRunStats(boundAgentIds);

  // The registry is served: until it answers, an id is not yet known,
  // which is not the same as absent. "Not found" only once it is loaded.
  if (!registryReady && !registryFailed) {
    return (
      <div className="space-y-6" data-testid="builtin-tool-detail-loading">
        <BackButton to="/tools" label="Back to Tools" />
        <PageSkeleton cards={2} />
      </div>
    );
  }

  if (registryFailed) {
    return (
      <div className="space-y-6">
        <BackButton to="/tools" label="Back to Tools" />
        <BuiltinToolsPending isError testId="builtin-tool-detail" />
      </div>
    );
  }

  if (!tool.known) {
    return (
      <div className="space-y-6">
        <BackButton to="/tools" label="Back to Tools" />
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-12 text-center">
            <Wrench className="h-10 w-10 text-default-secondary mb-3" />
            <CardTitle className="text-base mb-1">{t_i18n("Tool not found")}</CardTitle>
            <p className="content-base text-default-secondary">{t_i18n("This tool may not exist.")}</p>
            <Button className="mt-4" variant="secondary" onClick={() => navigate("/tools")}>{t_i18n("View All Tools")}</Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  const Icon = tool.icon;
  // Image generation is part of the ``files`` group.
  const hasModelConfig = isImageTool;
  const gate = byGroup.get(tool.id);

  const tabs: { key: typeof tab; label: string; icon: React.ElementType }[] = [
    { key: "overview", label: "Overview", icon: BarChart3 },
    { key: "config" as const, label: "Configuration", icon: Settings2 },
    { key: "activity", label: "Activity", icon: Activity },
  ];

  return (
    <div className="space-y-6">
      <EntityPageHeader
        backTo="/tools"
        backLabel="Back to Tools"
        icon={Icon}
        title={tool.label}
        badges={
          <>
            <Badge variant="cyan" className="content-caption gap-1"><Sparkles className="h-2.5 w-2.5" /> {t_i18n("Built-in")}</Badge>
            <span className="text-default-disabled">·</span>
            <Badge variant={tool.available ? "success" : "outline"} className="content-caption">
              {tool.available ? t_i18n("Enabled") : t_i18n("Disabled")}
            </Badge>
            {gate && <ToolGateBadge gating={gate.gating} total={gate.total} />}
            <CompanyManagedBadge size="default" />
          </>
        }
      />

      {/* Tabs */}
      <DetailTabs tabs={tabs} active={tab} />

      {/* Overview tab */}
      {tab === "overview" && (
        <div className="space-y-6">
          {/* Stats */}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard icon={Bot} label="Agents Using" value={boundAgents.length} subtitle={t_i18n("{count, plural, one {agent connected} other {agents connected}}", { count: boundAgents.length })} />
            <StatCard icon={BarChart3} label="Assignment Runs" value={totalRuns ?? "—"} subtitle="from agents using this tool" />
            <StatCard icon={Activity} label="Last Activity" value={lastRun ? formatShortDate(lastRun) : "—"} subtitle={lastRun ? formatTime(lastRun) : "No activity yet"} />
            <StatCard icon={Sparkles} label="Type" value={t_i18n("Built-in")} subtitle="Platform-provided tool" />
          </div>

          {/* Description */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="content-base flex items-center gap-2">
                <Icon className="h-4 w-4 text-accent" /> {t_i18n("Overview")}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="rounded-sm bg-elevation-hover p-3 space-y-1">
                <div className="content-compact font-medium text-default-secondary">{t_i18n("Description")}</div>
                <p className="content-base text-default-secondary">{tool.detail}</p>
              </div>
              {hasModelConfig && (
                <div className="rounded-sm bg-elevation-hover p-3 space-y-1">
                  <div className="content-compact font-medium text-default-secondary">{t_i18n("Current Model")}</div>
                  <p className="content-base text-default-secondary">{imageModelShown}</p>
                </div>
              )}
            </CardContent>
          </Card>

          {/* Agents using this tool */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="content-base flex items-center gap-2">
                <Bot className="h-4 w-4 text-accent" /> {t_i18n("Agents Using This Tool")}
                <CountBadge variant="cyan" className="ml-auto">{boundAgents.length}</CountBadge>
              </CardTitle>
            </CardHeader>
            <CardContent>
              {boundAgents.length === 0 ? (
                <div className="text-center py-6">
                  <Bot className="h-8 w-8 text-default-disabled mx-auto mb-2" />
                  <p className="content-base text-default-secondary">{t_i18n("No agents are using this tool yet.")}</p>
                  <p className="content-compact text-default-secondary mt-1">{t_i18n("Enable this built-in tool in an agent's settings.")}</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {boundAgents.map((agent) => {
                    const IconComp = ICON_MAP[agent.icon] ?? Bot;
                    return (
                      <button
                        key={agent.id}
                        onClick={() => navigate(`/agents/${agent.id}`)}
                        className="w-full flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5 text-left transition-colors hover:bg-elevation-highlight group"
                      >
                        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-sm bg-accent/10">
                          <IconComp className="h-4 w-4 text-accent" />
                        </div>
                        <div className="min-w-0 flex-1">
                          <p className="content-base font-medium truncate">{agent.name}</p>
                          <p className="content-caption text-default-secondary">
                            {formatCompactNumber(agent.assignment_count)} {t_i18n(pluralize(agent.assignment_count, "assignment"))}
                          </p>
                        </div>
                        <Badge variant={agent.enabled ? "success" : "outline"} className="text-[9px] shrink-0">
                          {agent.enabled ? t_i18n("Active") : t_i18n("Disabled")}
                        </Badge>
                        <ArrowRight className="h-3.5 w-3.5 text-default-disabled group-hover:text-accent transition-colors shrink-0" />
                      </button>
                    );
                  })}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {/* Configuration tab */}
      {tab === "config" && (
        <div className="space-y-6">
          <BuiltinToolsCard builtinId={tool.id} />

          {hasModelConfig && (
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="content-base flex items-center gap-2">
                  <Settings2 className="h-4 w-4 text-accent" /> {t_i18n("Configuration")}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                {isAdmin ? (
                  <div className="space-y-4">
                    <div className="space-y-2">
                      <label className="content-compact font-medium text-default-secondary">{t_i18n("Image Generation Model")}</label>
                      <p className="content-caption text-default-secondary">
                        {t_i18n("Model used for generating images. This setting applies platform-wide to all agents and the General Assistant.")}
                      </p>
                      <SearchableSelect
                        aria-label={t_i18n("Image Generation Model")}
                        options={(availableImageModels ?? []).map((m) => ({
                          value: m.model,
                          label: m.model,
                          description: PROVIDER_DISPLAY_NAMES[m.provider] || m.provider,
                          selectedLabel: `${m.model} (${PROVIDER_DISPLAY_NAMES[m.provider] || m.provider})`,
                        }))}
                        pinned={[{ value: "__none__", label: t_i18n("Auto (provider default)") }]}
                        placeholder={t_i18n("Search models...")}
                        value={gaConfig?.image_model || "__none__"}
                        onValueChange={(v) => {
                          const model = v === "__none__" ? "" : v;
                          const matchedModel = model
                            ? (availableImageModels ?? []).find((m) => m.model === model)
                            : undefined;
                          const prov = matchedModel?.provider ?? "";
                          const provId = matchedModel?.provider_id ?? null;
                          updateGAConfig.mutate({
                            image_provider: prov,
                            image_provider_id: provId,
                            image_model: model,
                          });
                        }}
                      />
                    </div>
                    <div className="rounded-sm bg-elevation-hover p-3 flex items-start gap-2.5">
                      <Building2 className="h-3.5 w-3.5 text-accent shrink-0 mt-0.5" />
                      <p className="content-compact text-default-secondary leading-relaxed">
                        {t_i18n("This is a company-managed built-in tool. The image generation model is a platform-wide setting configured by admins.")}
                      </p>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-4">
                    <div className="rounded-sm bg-elevation-hover p-3 space-y-1">
                      <div className="content-compact font-medium text-default-secondary">{t_i18n("Image Generation Model")}</div>
                      <p className="content-base text-default-secondary">{imageModelShown}</p>
                    </div>
                    <CompanyManagedBanner entityLabel="custom tool" />
                  </div>
                )}
              </CardContent>
            </Card>
          )}
        </div>
      )}

      {/* Activity tab */}
      {tab === "activity" && <EntityActivityTab boundAgentIds={boundAgentIds} entityLabel="custom tool" />}
    </div>
  );
}
