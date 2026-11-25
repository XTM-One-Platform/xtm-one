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

import {
  Bot,
  Braces,
  ChevronDown,
  Database,
  FileText,
  GraduationCap,
  Puzzle,
  Server,
  Wrench,
  Zap,
} from "lucide-react";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useParams } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { usePublicCatalogAgent } from "@/hooks/api/usePublicCatalogApi";
import { TRIGGER_ICONS, TRIGGER_LABELS, ICON_MAP } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";

import {
  CatalogDetailSkeleton,
  DetailShell,
  CatalogSetupWizard,
  IncludedBadge,
  IntegrationBadge,
  ResourceBadge,
} from "./components";

const PERSONA_COLLAPSED_HEIGHT = 200;

export default function PublicAgentDetail() {
  const { t_i18n } = useFormatter();
  const { id } = useParams<{ id: string }>();
  const { data: agent, isLoading } = usePublicCatalogAgent(id!);
  const [personaExpanded, setPersonaExpanded] = useState(false);
  const [showWizard, setShowWizard] = useState(false);

  if (isLoading) return <CatalogDetailSkeleton backTo="/catalog/agents" backLabel="Back to Agents" />;
  if (!agent)
    return (
      <p className="text-center text-default-secondary py-20">
        {t_i18n("Agent not found")}
      </p>
    );

  const IconComp = ICON_MAP[agent.icon] ?? Bot;
  const hasOverview =
    (agent.integration_providers?.length ?? 0) > 0 ||
    (agent.skills?.length ?? 0) > 0 ||
    (agent.prompts?.length ?? 0) > 0 ||
    (agent.knowledge_bases?.length ?? 0) > 0 ||
    (agent.variables?.length ?? 0) > 0 ||
    (agent.mcp_servers?.length ?? 0) > 0 ||
    (agent.custom_tools?.length ?? 0) > 0 ||
    (agent.builtin_tool_ids?.length ?? 0) > 0;

  return (
    <>
      <DetailShell
        backTo="/catalog/agents"
        backLabel="Back to Agents"
        entityType="agents"
        entityId={agent.id}
        icon={<IconComp className="h-6 w-6 text-accent" />}
        name={agent.name}
        description={agent.description}
        tags={agent.tags}
        createdAt={agent.created_at}
        updatedAt={agent.updated_at}
        sharedBy={agent.shared_by}
        onInstall={() => setShowWizard(true)}
        downloadOmittedTools={agent.download_omitted_custom_tools}
      >
        {/* Persona */}
        {agent.persona && (
          <div className="relative overflow-hidden rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-5 backdrop-blur-sm space-y-2">
            <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/20 to-transparent" />
            <h2 className="font-sans-geologica content-compact font-semibold text-default-secondary uppercase tracking-wider">
              {t_i18n("Persona")}
            </h2>
            <div
              className={cn(
                "relative content-base leading-relaxed prose prose-invert prose-sm max-w-none",
                "prose-headings:text-default-primary prose-p:text-default-secondary/90",
                "prose-strong:text-default-primary prose-li:text-default-secondary/90",
                "prose-code:text-accent prose-code:bg-elevation-hover prose-code:rounded-sm prose-code:px-1",
                !personaExpanded && "overflow-hidden",
              )}
              style={
                !personaExpanded
                  ? { maxHeight: PERSONA_COLLAPSED_HEIGHT }
                  : undefined
              }
            >
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {agent.persona}
              </ReactMarkdown>
              {!personaExpanded && (
                <div className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-[var(--bg-elevation-default)] to-transparent" />
              )}
            </div>
            <button
              onClick={() => setPersonaExpanded(!personaExpanded)}
              className="flex items-center gap-1 content-compact text-accent hover:text-accent/80 transition-colors"
            >
              <ChevronDown
                className={cn(
                  "h-3.5 w-3.5 transition-transform",
                  personaExpanded && "rotate-180",
                )}
              />
              {personaExpanded ? t_i18n("Collapse") : t_i18n("Expand full persona")}
            </button>
          </div>
        )}

        {/* Overview with binding status */}
        {hasOverview && (
          <div className="relative overflow-hidden rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-5 backdrop-blur-sm space-y-3">
            <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/20 to-transparent" />
            <h2 className="font-sans-geologica content-compact font-semibold text-default-secondary uppercase tracking-wider">
              {t_i18n("Overview")}
            </h2>
            <div className="grid gap-2">
              {(agent.integration_providers?.length ?? 0) > 0 && (
                <OverviewRow
                  icon={<Puzzle className="h-3.5 w-3.5" />}
                  label="Integrations"
                >
                  <div className="flex flex-wrap gap-1.5">
                    {agent.integration_providers!.map((ip, i) => (
                      <IntegrationBadge
                        key={i}
                        provider={ip.provider}
                        name={ip.name}
                        isCompanyManaged={ip.is_company_managed}
                      />
                    ))}
                  </div>
                </OverviewRow>
              )}
              {(agent.knowledge_bases?.length ?? 0) > 0 && (
                <OverviewRow
                  icon={<Database className="h-3.5 w-3.5" />}
                  label="Knowledge Bases"
                >
                  <div className="flex flex-wrap gap-1.5">
                    {agent.knowledge_bases!.map((kb) =>
                      kb.is_company_managed
                        ? <ResourceBadge key={kb.id} name={kb.name} isCompanyManaged description={kb.description} />
                        : <IncludedBadge key={kb.id} name={kb.name} setupNote="needs documents" />
                    )}
                  </div>
                </OverviewRow>
              )}
              {(agent.mcp_servers?.length ?? 0) > 0 && (
                <OverviewRow
                  icon={<Server className="h-3.5 w-3.5" />}
                  label="MCP Servers"
                >
                  <div className="flex flex-wrap gap-1.5">
                    {agent.mcp_servers!.map((s) => (
                      <IncludedBadge key={s.id} name={s.name} setupNote="env vars needed" />
                    ))}
                  </div>
                </OverviewRow>
              )}
              {(agent.custom_tools?.length ?? 0) > 0 && (
                <OverviewRow
                  icon={<Wrench className="h-3.5 w-3.5" />}
                  label="Custom Tools"
                >
                  <div className="flex flex-wrap gap-1.5">
                    {agent.custom_tools!.map((t) => (
                      <ResourceBadge
                        key={t.id}
                        name={t.name}
                        isCompanyManaged={t.is_company_managed}
                      />
                    ))}
                  </div>
                </OverviewRow>
              )}
              {(agent.skills?.length ?? 0) > 0 && (
                <OverviewRow
                  icon={<GraduationCap className="h-3.5 w-3.5" />}
                  label="Skills"
                >
                  <div className="flex flex-wrap gap-1.5">
                    {agent.skills!.map((sk) => (
                      <IncludedBadge key={sk.id} name={sk.name} />
                    ))}
                  </div>
                </OverviewRow>
              )}
              {(agent.prompts?.length ?? 0) > 0 && (
                <OverviewRow
                  icon={<FileText className="h-3.5 w-3.5" />}
                  label="Prompts"
                >
                  <div className="flex flex-wrap gap-1.5">
                    {agent.prompts!.map((pr) => (
                      <IncludedBadge key={pr.id} name={pr.name} />
                    ))}
                  </div>
                </OverviewRow>
              )}
              {(agent.builtin_tool_ids?.length ?? 0) > 0 && (
                <OverviewRow
                  icon={<Wrench className="h-3.5 w-3.5" />}
                  label="Built-in Tools"
                >
                  {agent.builtin_tool_ids!.map((tid, i) => (
                    <Badge key={i} variant="secondary" className="content-caption">
                      {tid}
                    </Badge>
                  ))}
                </OverviewRow>
              )}
              {(agent.variables?.length ?? 0) > 0 && (
                <OverviewRow
                  icon={<Braces className="h-3.5 w-3.5" />}
                  label="Variables"
                >
                  {agent.variables!.map((v, i) => (
                    <Badge
                      key={i}
                      variant="outline"
                      className="content-caption font-mono-plex"
                    >
                      {v.key}
                    </Badge>
                  ))}
                </OverviewRow>
              )}
            </div>
          </div>
        )}

        {/* Assignments */}
        {agent.assignments.length > 0 && (
          <div className="relative overflow-hidden rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-5 backdrop-blur-sm space-y-3">
            <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/20 to-transparent" />
            <h2 className="font-sans-geologica content-compact font-semibold text-default-secondary uppercase tracking-wider">
              {t_i18n("Assignments ({count})", { count: agent.assignments.length })}
            </h2>
            <div className="divide-y divide-[var(--border-elevation-subtle)]">
              {agent.assignments.map((w, i) => {
                const TriggerIcon =
                  TRIGGER_ICONS[
                    w.trigger_type as keyof typeof TRIGGER_ICONS
                  ] ?? Zap;
                return (
                  <div key={i} className="py-3 first:pt-0 last:pb-0 space-y-1">
                    <div className="flex items-center gap-2">
                      <TriggerIcon className="h-3.5 w-3.5 text-accent/60" />
                      <span className="content-base font-medium">{w.name}</span>
                      <span className="rounded-sm border border-transparent bg-elevation-hover px-2 py-0.5 content-caption text-default-secondary">
                        {TRIGGER_LABELS[
                          w.trigger_type as keyof typeof TRIGGER_LABELS
                        ] ?? w.trigger_type}
                      </span>
                    </div>
                    {w.description && (
                      <p className="content-compact text-default-secondary ml-5.5">
                        {w.description}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </DetailShell>

      {/* Setup wizard */}
      <CatalogSetupWizard
        open={showWizard}
        onClose={() => setShowWizard(false)}
        entityType="agents"
        entityId={agent.id}
        entityName={agent.name}
        entityIcon={agent.icon}
        integrationProviders={agent.integration_providers}
        knowledgeBases={agent.knowledge_bases}
        mcpServers={agent.mcp_servers}
        customTools={agent.custom_tools}
        skills={agent.skills}
        prompts={agent.prompts}
        assignmentCount={agent.assignments.length}
      />
    </>
  );
}

function OverviewRow({
  icon,
  label,
  children,
}: {
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex items-start gap-3 py-1.5">
      <div className="flex items-center gap-2 shrink-0 w-32">
        <span className="text-accent/60">{icon}</span>
        <span className="content-compact text-default-secondary font-medium">
          {t_i18n(label)}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-1.5">{children}</div>
    </div>
  );
}
