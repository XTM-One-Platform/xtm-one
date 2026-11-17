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
import { useParams, useNavigate } from "react-router-dom";
import { useTabParam } from "@/hooks/useTabParam";
import {
  LayoutTemplate, Trash2, Settings2, BarChart3, Paperclip, Play, Download, Copy, Moon, Sparkles, Sun, Clapperboard,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import { useToast } from "@/components/ui/toast";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { BuiltinBadge } from "@/components/ui/builtin-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { NotFoundState } from "@/components/ui/not-found-state";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { EntityHeaderMoreActions, EntityHeaderWideOnly, EntityPageHeader } from "@/components/ui/entity-page-header";
import { useAuth } from "@/hooks/useAuth";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import {
  useTemplateContainer,
  useDeleteTemplateContainer,
  useDuplicateTemplateContainer,
  useExportTemplateContainer,
} from "@/hooks/api/useTemplateContainerApi";
import { templateKindLabel } from "@/lib/constants";
import { isEntityShared, canManageEntity, pluralize } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { TemplatePreviewDialog } from "@/components/presentation/TemplatePreviewDialog";
import { NewPresentationDialog } from "@/components/presentation/NewPresentationDialog";
import { useSchemeLabel } from "@/components/presentation/scheme-label";
import { NewVideoDialog } from "@/components/video-studio/NewVideoDialog";
import { OverviewTab } from "./template-detail/OverviewTab";
import { ConfigTab } from "./template-detail/ConfigTab";
import { AssetsTab } from "./template-detail/AssetsTab";
import { useAiActionDisabled, useAiDisabledReason } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { AgenticGateDialog, AgenticEEBadgeForButton } from "@/components/ui/license-gate-dialog";

type Tab = "overview" | "config" | "assets";

/** One width for the icon-only header actions below xl: an outline
 *  button is 32 px (its 1 px border included), the filled one had no border
 *  and drew 30 px. A floor at the outline width lines the three up and leaves
 *  the labelled form at xl untouched. */
const ICON_ONLY_MIN_W = "min-w-8 justify-center";

export default function TemplateDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const { user, isAdmin } = useAuth();
  const { data: template, isLoading } = useTemplateContainer(id);
  const deleteTemplate = useDeleteTemplateContainer();
  const aiOff = useAiActionDisabled();
  const aiOffReason = useAiDisabledReason() ?? undefined;
  const [tab] = useTabParam<Tab>("overview");
  const [ConfirmEl, confirm] = useConfirm();
  const [previewOpen, setPreviewOpen] = useState(false);
  // "Use this template": the New presentation dialog with this
  // template already picked — the deck is generated in the chat, dressed in it.
  const [creating, setCreating] = useState(false);
  const [creatingVideo, setCreatingVideo] = useState(false);
  // A video is made behind `VIDEO_STUDIO`: off, a video template is
  // read, duplicated and exported, never used.
  const videoStudio = useFeatureFlag("VIDEO_STUDIO");
  const schemeLabel = useSchemeLabel();

  const isShared = isEntityShared(template, user?.id);
  // A built-in is read-only for EVERYONE, admins included: the seed
  // full-syncs every field on boot, so an edit would be reverted by the next
  // deploy. The REST API refuses the write with a 403 as well — this only keeps
  // the UI from offering a form that cannot be saved.
  const canManage = canManageEntity(template, user?.id, isAdmin) && !template?.is_builtin;
  const exportTemplate = useExportTemplateContainer();
  const duplicateTemplate = useDuplicateTemplateContainer();

  // Export and Duplicate are READS of the source, so both are offered to anyone
  // who can see the template — not gated on `canManage`. Duplicating (or
  // exporting and importing your own copy) is the documented way to get an
  // editable version of a company-managed or built-in template.
  const handleDuplicate = () => {
    if (!template) return;
    duplicateTemplate.mutate(template.id, {
      onSuccess: (copy) => {
        toast({ title: t_i18n("Template duplicated"), variant: "success" });
        navigate(`/templates/${copy.id}/config`);
      },
      onError: (err) => {
        toast({
          title: t_i18n("Duplicate failed"),
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        });
      },
    });
  };

  const handleExport = async () => {
    if (!template) return;
    try {
      await exportTemplate.mutateAsync({
        containerId: template.id,
        name: template.name,
        format: "zip",
      });
    } catch (err) {
      toast({
        title: t_i18n("Export failed"),
        description: err instanceof Error ? err.message : t_i18n("Export failed"),
        variant: "error",
      });
    }
  };

  const handleDelete = async () => {
    if (!template) return;
    const assetCount = template.assets?.length ?? 0;
    if (
      await confirm({
        title: "Delete Template",
        // A deck wearing the template is not deleted with it: the link is
        // released (FK SET NULL) and the deck keeps its content in the plain
        // look — said here because "delete" could be read as taking the decks.
        description:
          assetCount > 0
            ? `Are you sure you want to delete "${template.name}"? Its ${assetCount} ${pluralize(assetCount, "asset")} will be permanently deleted too. Presentations wearing it keep their content and fall back to the plain look.`
            : `Are you sure you want to delete "${template.name}"? Presentations wearing it keep their content and fall back to the plain look. Agents will no longer find it.`,
        confirmLabel: "Delete",
        variant: "danger",
      })
    ) {
      deleteTemplate.mutate(template.id, {
        onSuccess: () => {
          toast({ title: t_i18n("Template deleted"), variant: "success" });
          navigate("/templates");
        },
      });
    }
  };

  if (isLoading) {
    return <DetailPageSkeleton backTo="/templates" backLabel="Back to Templates" tabs={3} actions={1} />;
  }

  if (!template) {
    return (
      <NotFoundState
        icon={LayoutTemplate}
        title="Template not found"
        description="This template may have been deleted or you don't have access."
        actionLabel="View All Templates"
        onAction={() => navigate("/templates")}
        backTo="/templates"
        backLabel="Back to Templates"
      />
    );
  }

  const assetCount = template.assets?.length ?? 0;
  const tabs: { key: Tab; label: string; icon: React.ElementType }[] = [
    { key: "overview", label: "Overview", icon: BarChart3 },
    { key: "config", label: "Configuration", icon: Settings2 },
    { key: "assets", label: "Assets", icon: Paperclip },
  ];

  return (
    <div className="space-y-6">
      {ConfirmEl}

      <EntityPageHeader
        backTo="/templates"
        backLabel="Back to Templates"
        icon={LayoutTemplate}
        title={template.name}
        badges={
          <>
                <span className="content-base text-default-secondary">{t_i18n("Template")}</span>
                <span className="text-default-disabled">·</span>
                <Badge variant="cyan" className="content-caption">{t_i18n(templateKindLabel(template.kind))}</Badge>
                {template.aspect_ratio && (
                  <Badge variant="outline" className="content-caption">{template.aspect_ratio}</Badge>
                )}
                {/* The themes this design can dress a deck in: a
                    dark-only brand shows "Dark" and its decks cannot go light. */}
                {template.supported_schemes.map((scheme) => (
                  <Badge key={scheme} variant="outline" className="content-caption gap-1">
                    {scheme === "dark" ? <Moon className="h-2.5 w-2.5" /> : <Sun className="h-2.5 w-2.5" />}
                    {schemeLabel(scheme)}
                  </Badge>
                ))}
                {assetCount > 0 && (
                  <Badge variant="outline" className="content-caption">
                    {assetCount} {t_i18n(pluralize(assetCount, "asset"))}
                  </Badge>
                )}
                {template.is_builtin && <BuiltinBadge />}
                {template.is_company_managed && <CompanyManagedBadge size="default" />}
                {isShared && <SharedBadge size="default" ownerName={template.owner_name} />}
                {(template.is_builtin || (template.is_company_managed && !isAdmin) || (isShared && !canManage)) && <ReadOnlyBadge />}
          </>
        }
        actions={
          <>
            {/* Presentation templates are the only kind with a renderer today;
                a report or dashboard template has nothing to preview. */}
            {/* Secondary actions first, the filled ones at the right end, the
                icon-only delete last (design-system doc, "Page-title actions").
                Below xl the secondary actions show their icon only,
                so at 1024 the title keeps its width and the badges wrap to fewer rows.
                ICON_ONLY_MIN_W gives Preview, Export and Duplicate one width there.
                Duplicate is outline on every template, built-in or not: the create
                action is the one filled primary and the Read-only badge carries the
                built-in state, not the button style (UX Director ruling). */}
            {template.kind === "presentation" && (
              <Button variant="outline" size="sm" className={ICON_ONLY_MIN_W} onClick={() => setPreviewOpen(true)} aria-label={t_i18n("Preview")} title={t_i18n("Preview")}>
                <Play className="h-3.5 w-3.5" />
                <span className="hidden xl:inline">{t_i18n("Preview")}</span>
              </Button>
            )}
            <EntityHeaderWideOnly>
            <Button
              variant="outline"
              size="sm"
              className={ICON_ONLY_MIN_W}
              onClick={handleExport}
              disabled={exportTemplate.isPending}
              aria-label={t_i18n("Export")}
              title={t_i18n("Export")}
            >
              <Download className="h-3.5 w-3.5" />
              <span className="hidden xl:inline">{t_i18n("Export")}</span>
            </Button>
            <AiDisabledTip><Button
              variant="outline"
              size="sm"
              className={ICON_ONLY_MIN_W}
              onClick={handleDuplicate}
              disabled={duplicateTemplate.isPending || aiOff}
              aria-label={t_i18n("Duplicate")}
              title={t_i18n("Duplicate")}
            >
              <Copy className="h-3.5 w-3.5" />
              <span className="hidden xl:inline">{t_i18n("Duplicate")}</span>
            </Button></AiDisabledTip>
            </EntityHeaderWideOnly>
            {template.kind === "presentation" && (
              <AgenticGateDialog feature={t_i18n("Presentations")}>
                <Button size="sm" className="gap-1.5" onClick={() => setCreating(true)} title={t_i18n("New presentation with this template")}>
                  <Sparkles className="h-3.5 w-3.5" />
                  <span className="xl:hidden">{t_i18n("New presentation")}</span>
                  <span className="hidden xl:inline">{t_i18n("New presentation with this template")}</span>
                  <AgenticEEBadgeForButton />
                </Button>
              </AgenticGateDialog>
            )}
            {template.kind === "video" && videoStudio && (
              <AgenticGateDialog feature={t_i18n("Producing videos")}>
                <Button size="sm" className="gap-1.5" onClick={() => setCreatingVideo(true)}>
                  <Clapperboard className="h-3.5 w-3.5" />
                  {t_i18n("New video with this template")}
                  <AgenticEEBadgeForButton />
                </Button>
              </AgenticGateDialog>
            )}
            {canManage && (
              <EntityHeaderWideOnly>
              <AiDisabledTip><Button aria-label={t_i18n("Delete template")}
                variant="ghost"
                size="sm"
                className="h-8 w-8 p-0 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10"
                onClick={handleDelete}
                disabled={deleteTemplate.isPending || aiOff}
              >
                <Trash2 className="h-4 w-4" />
              </Button></AiDisabledTip>
              </EntityHeaderWideOnly>
            )}
            {/* Below 1280 px: Duplicate, Export and Delete fold into
                the "More actions" menu. */}
            <EntityHeaderMoreActions
              items={[
                { id: "duplicate", label: t_i18n("Duplicate"), icon: Copy, onSelect: handleDuplicate, disabled: duplicateTemplate.isPending || aiOff, disabledReason: aiOff ? aiOffReason : undefined },
                { id: "export", label: t_i18n("Export"), icon: Download, onSelect: () => void handleExport(), disabled: exportTemplate.isPending },
                ...(canManage
                  ? [{ id: "delete", label: t_i18n("Delete template"), icon: Trash2, destructive: true, separatorBefore: true, onSelect: () => void handleDelete(), disabled: deleteTemplate.isPending || aiOff, disabledReason: aiOff ? aiOffReason : undefined }]
                  : []),
              ]}
            />
          </>
        }
      />

      <DetailTabs tabs={tabs} active={tab} />

      {tab === "overview" && <OverviewTab template={template} />}
      {tab === "config" && <ConfigTab template={template} canManage={!!canManage} />}
      {tab === "assets" && <AssetsTab template={template} canManage={!!canManage} />}

      {/* Mounted only while open so reveal.js is initialized on demand. */}
      {previewOpen && (
        <TemplatePreviewDialog
          template={template}
          open={previewOpen}
          onOpenChange={setPreviewOpen}
        />
      )}
      {template.kind === "presentation" && (
        <NewPresentationDialog open={creating} onOpenChange={setCreating} templateId={template.id} />
      )}
      {template.kind === "video" && videoStudio && (
        <NewVideoDialog open={creatingVideo} onOpenChange={setCreatingVideo} templateId={template.id} />
      )}
    </div>
  );
}
