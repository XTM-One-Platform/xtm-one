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

import { useParams, useNavigate } from "react-router-dom";
import { useTabParam } from "@/hooks/useTabParam";
import {
  FileText, Trash2,
  Settings2, BarChart3, Activity, History,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import { useToast } from "@/components/ui/toast";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { NotFoundState } from "@/components/ui/not-found-state";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { useAuth } from "@/hooks/useAuth";
import {
  usePrompt, useDeletePrompt,
} from "@/hooks/api/usePromptApi";
import { isEntityShared, canManageEntity } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { NewPromptForm } from "./prompt-detail/NewPromptForm";
import { OverviewTab } from "./prompt-detail/OverviewTab";
import { ConfigTab } from "./prompt-detail/ConfigTab";
import { ActivityTab } from "./prompt-detail/ActivityTab";
import { VersioningTab } from "./prompt-detail/VersioningTab";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";

type Tab = "overview" | "config" | "versioning" | "activity";

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

export default function PromptDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const { user, isAdmin } = useAuth();
  const { data: prompt, isLoading } = usePrompt(id === "new" ? "" : id ?? "");
  const deletePrompt = useDeletePrompt();
  const aiOff = useAiActionDisabled();
  const [tab] = useTabParam<Tab>("overview");
  const [ConsentEl, confirmSharing] = useCredentialSharingConfirm();

  if (id === "new") {
    return <NewPromptForm />;
  }
  const isShared = isEntityShared(prompt, user?.id);
  const canManage = canManageEntity(prompt, user?.id, isAdmin);

  const handleDelete = async () => {
    if (!prompt) return;
    if (await confirmSharing({ entity_type: "prompt", entity_id: prompt.id, change: { delete: true } }, { mode: "delete", fallback: { title: "Delete Prompt", description: `Are you sure you want to delete "${prompt.name}"? Agents using this prompt will lose access.` } })) {
      deletePrompt.mutate(prompt.id, {
        onSuccess: () => {
          toast({ title: t_i18n("Prompt deleted"), variant: "success" });
          navigate("/prompts");
        },
      });
    }
  };

  if (isLoading) {
    return <DetailPageSkeleton backTo="/prompts" backLabel="Back to Prompts" tabs={4} actions={1} />;
  }

  if (!prompt) {
    return (
      <NotFoundState
        icon={FileText}
        title="Prompt not found"
        description="This prompt may have been deleted or you don't have access."
        actionLabel="View All Prompts"
        onAction={() => navigate("/prompts")}
        backTo="/prompts"
        backLabel="Back to Prompts"
      />
    );
  }

  const tabs: { key: Tab; label: string; icon: React.ElementType }[] = [
    { key: "overview", label: "Overview", icon: BarChart3 },
    { key: "config", label: "Configuration", icon: Settings2 },
    { key: "versioning", label: "Versioning", icon: History },
    { key: "activity", label: "Activity", icon: Activity },
  ];

  return (
    <div className="space-y-6">
      {ConsentEl}

      <EntityPageHeader
        backTo="/prompts"
        backLabel="Back to Prompts"
        icon={FileText}
        title={prompt.name}
        badges={
          <>
                <span className="content-base text-default-secondary">{t_i18n("Prompt Template")}</span>
                {prompt.content && (
                  <>
                    <span className="text-default-disabled">·</span>
                    <Badge variant="outline" className="content-caption">
                      {t_i18n("{count} chars", { count: prompt.content.length })}
                    </Badge>
                  </>
                )}
                {prompt.is_company_managed && <CompanyManagedBadge size="default" />}
                {isShared && <SharedBadge size="default" ownerName={prompt.owner_name} />}
                {((prompt.is_company_managed && !isAdmin) || (isShared && !canManage)) && (
                  <ReadOnlyBadge />
                )}
          </>
        }
        actions={
          canManage && (
              <AiDisabledTip><Button aria-label={t_i18n("Delete prompt")} variant="ghost" size="sm" className="h-8 w-8 p-0 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10" onClick={handleDelete} disabled={deletePrompt.isPending || aiOff}>
                <Trash2 className="h-4 w-4" />
              </Button></AiDisabledTip>
          )
        }
      />

      {/* Tabs */}
      <DetailTabs tabs={tabs} active={tab} />

      {tab === "overview" && <OverviewTab prompt={prompt} />}
      {tab === "config" && <ConfigTab prompt={prompt} canManage={!!canManage} />}
      {tab === "versioning" && <VersioningTab prompt={prompt} canManage={!!canManage} isAdmin={isAdmin} />}
      {tab === "activity" && <ActivityTab prompt={prompt} />}
    </div>
  );
}
