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
import { Link, useParams, useNavigate } from "react-router-dom";
import { useTabParam } from "@/hooks/useTabParam";
import {
  GraduationCap, Trash2, RefreshCw, Download,
  Activity, BookOpenText, FileText, Wrench, History, AlertTriangle, KeyRound, GitFork,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import { useToast } from "@/components/ui/toast";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { NotFoundState } from "@/components/ui/not-found-state";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { Tip } from "@/components/ui/tooltip";
import { useAuth } from "@/hooks/useAuth";
import { exportSkillPackage, exportSkillJson, useSkill, useDeleteSkill, useRefreshSkill } from "@/hooks/api/useSkillApi";
import { cn, isEntityShared, canManageEntity } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { NewSkillForm } from "./skill-detail/NewSkillForm";
import { ActivityTab } from "./skill-detail/ActivityTab";
import { CatalogTab } from "./skill-detail/CatalogTab";
import { InstructionsTab } from "./skill-detail/InstructionsTab";
import { ResourcesTab } from "./skill-detail/ResourcesTab";
import { VersioningTab } from "./skill-detail/VersioningTab";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { DuplicateButton } from "@/components/ui/duplicate-button";
import { ForkSkillDialog } from "./skill-detail/ForkSkillDialog";
import { skillForkProvenance } from "./skill-detail/fork-skill-form";

type Tab = "catalog" | "instructions" | "resources" | "versioning" | "activity";

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

export default function SkillDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const { user, isAdmin } = useAuth();
  const { data: skill, isLoading } = useSkill(id === "new" ? undefined : id);
  const deleteSkill = useDeleteSkill();
  const aiOff = useAiActionDisabled();
  const refreshSkill = useRefreshSkill();
  const [tab] = useTabParam<Tab>("catalog");
  const [ConfirmEl, confirm] = useConfirm();
  const [ConsentEl, confirmSharing] = useCredentialSharingConfirm();
  // Duplicate (#9174): fork any skill, a built-in one included, into an editable copy.
  const [forkOpen, setForkOpen] = useState(false);

  if (id === "new") {
    return <NewSkillForm />;
  }

  const isShared = isEntityShared(skill, user?.id);
  const isBuiltinSkill = skill?.tags?.includes("builtin") ?? false;
  const canRefresh = canManageEntity(skill, user?.id, isAdmin);
  const canManage = isBuiltinSkill ? false : canRefresh;
  const canDelete = !isBuiltinSkill && (isAdmin || (skill?.user_id != null && skill.user_id === user?.id));

  const handleDelete = async () => {
    if (!skill) return;
    if (await confirmSharing({ entity_type: "skill", entity_id: skill.id, change: { delete: true } }, { mode: "delete", fallback: { title: "Delete Skill", description: `Are you sure you want to delete "${skill.name}"? Agents using this skill will lose access.` } })) {
      deleteSkill.mutate(skill.id, {
        onSuccess: () => {
          toast({ title: t_i18n("Skill deleted"), variant: "success" });
          navigate("/skills");
        },
      });
    }
  };

  const handleRefresh = async () => {
    if (!skill?.url) return;
    refreshSkill.mutate(skill.id, {
      onSuccess: () => toast({ title: t_i18n("Skill refreshed"), description: t_i18n("Content has been re-fetched from the URL."), variant: "success" }),
    });
  };

  const handleExport = async () => {
    if (!skill) return;
    if (!skill.package_exportable) {
      const warnings = skill.frontmatter_warnings.join(" ");
      const fallback = await confirm({
        title: "Package export unavailable",
        description: `${warnings} You can export as raw JSON instead, or fix the SKILL.md frontmatter and retry.`,
        confirmLabel: "Export as JSON",
        variant: "default",
      });
      if (fallback) {
        try {
          await exportSkillJson(skill.id, skill.name);
        } catch (err) {
          const message = err instanceof Error ? err.message : t_i18n("Export failed");
          toast({ title: t_i18n("Export failed"), description: message, variant: "error" });
        }
      }
      return;
    }
    try {
      await exportSkillPackage(skill.id, skill.name);
    } catch (err) {
      const message = err instanceof Error ? err.message : t_i18n("Export failed");
      toast({ title: t_i18n("Export failed"), description: message, variant: "error" });
    }
  };

  if (isLoading) {
    return <DetailPageSkeleton backTo="/skills" backLabel="Back to Skills" tabs={5} actions={1} />;
  }

  if (!skill) {
    return (
      <NotFoundState
        icon={GraduationCap}
        title="Skill not found"
        description="This skill may have been deleted or you don't have access."
        actionLabel="View All Skills"
        onAction={() => navigate("/skills")}
        backTo="/skills"
        backLabel="Back to Skills"
      />
    );
  }

  const provenance = skillForkProvenance(skill);

  const tabs: { key: Tab; label: string; icon: React.ElementType }[] = [
    { key: "catalog", label: "Catalog", icon: BookOpenText },
    { key: "instructions", label: "Instructions", icon: FileText },
    { key: "resources", label: "Resources", icon: Wrench },
    { key: "versioning", label: "Versioning", icon: History },
    { key: "activity", label: "Activity", icon: Activity },
  ];

  return (
    <div className="space-y-6">
      {ConfirmEl}
      {ConsentEl}

      <EntityPageHeader
        backTo="/skills"
        backLabel="Back to Skills"
        icon={GraduationCap}
        title={skill.name}
        badges={
          <>
                <span className="content-base text-default-secondary">
                  {skill.url ? t_i18n("URL-based Skill") : t_i18n("Direct Content Skill")}
                </span>
                <span className="text-default-disabled">·</span>
                <Badge variant={skill.enabled ? "success" : "outline"} className="content-caption">
                  {skill.enabled ? t_i18n("Enabled") : t_i18n("Disabled")}
                </Badge>
                {skill.is_company_managed && <CompanyManagedBadge size="default" />}
                {isShared && <SharedBadge size="default" ownerName={skill.owner_name} />}
                {!canManage && <ReadOnlyBadge />}
                {skill.source_integration_id && skill.auth_failure_count > 0 && (
                  <Tip content={t_i18n("The GitHub credential attached to this skill is being rejected, so the cached content is no longer being updated. Re-authorize the integration or attach another one: auto-refresh keeps retrying daily and recovers on its own once the credential works.")}>
                    <Badge variant="outline" className="content-caption text-feedback-error-primary border-feedback-error-primary/30 gap-1">
                      <KeyRound className="h-3 w-3" />
                      {t_i18n("Credential rejected")}
                    </Badge>
                  </Tip>
                )}
                {!skill.package_exportable && (
                  <Tip content={skill.frontmatter_warnings.join(" ")}>
                    <Badge variant="outline" className="content-caption text-feedback-warning-primary border-feedback-alert-primary/30 gap-1">
                      <AlertTriangle className="h-3 w-3" />
                      {t_i18n("Package export unavailable")}
                    </Badge>
                  </Tip>
                )}
                {provenance && (
                  <span className="flex min-w-0 items-center gap-1 content-caption text-default-secondary" data-testid="skill-provenance">
                    <GitFork className="h-3 w-3 shrink-0" aria-hidden />
                    <span className="truncate">
                      {provenance.isBuiltin
                        ? t_i18n("Forked from the built-in skill")
                        : t_i18n("Forked from")}{" "}
                      <Link to={`/skills/${provenance.sourceId}`} className="text-accent underline-offset-2 hover:underline focus-visible:underline">
                        {provenance.name}
                      </Link>
                      {provenance.version !== null && (
                        <> {t_i18n("(version {version})", { version: provenance.version })}</>
                      )}
                    </span>
                  </span>
                )}
          </>
        }
        actions={
          <>
            <DuplicateButton onDuplicate={() => setForkOpen(true)} hint={t_i18n("Create an editable copy of this skill")} />
            <Button
              variant="outline"
              size="sm"
              className="gap-1.5"
              onClick={handleExport}
            >
              <Download className="h-3.5 w-3.5" />
              {t_i18n("Export")}
            </Button>
            {/* A refresh rewrites the cached content: the API wants a manage
                right, which an admin holds on a built-in too. */}
            {skill.url && canRefresh && (
              <AiDisabledTip><Button variant="secondary" size="sm" className="gap-1.5" onClick={handleRefresh} disabled={refreshSkill.isPending || aiOff}>
                <RefreshCw className={cn("h-3.5 w-3.5", refreshSkill.isPending && "animate-spin")} />
                {t_i18n("Refresh")}
              </Button></AiDisabledTip>
            )}
            {canDelete && (
              <AiDisabledTip><Button aria-label={t_i18n("Delete skill")} variant="ghost" size="sm" className="h-8 w-8 p-0 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10" onClick={handleDelete} disabled={deleteSkill.isPending || aiOff}>
                <Trash2 className="h-4 w-4" />
              </Button></AiDisabledTip>
            )}
          </>
        }
      />

      {forkOpen && <ForkSkillDialog skill={skill} isBuiltin={isBuiltinSkill} open={forkOpen} onOpenChange={setForkOpen} />}

      {/* Tabs */}
      <DetailTabs tabs={tabs} active={tab} />

      {tab === "catalog" && <CatalogTab skill={skill} canManage={!!canManage} />}
      {tab === "instructions" && <InstructionsTab skill={skill} canManage={!!canManage} />}
      {tab === "resources" && <ResourcesTab skillId={skill.id} />}
      {tab === "versioning" && <VersioningTab skill={skill} canManage={!!canManage} isAdmin={isAdmin} />}
      {tab === "activity" && <ActivityTab skill={skill} />}
    </div>
  );
}
