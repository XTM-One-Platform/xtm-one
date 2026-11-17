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

import { useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Plus, GraduationCap, Link as LinkIcon, FileText, Upload, KeyRound,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { CardGridSkeleton, cardSkeletonCount } from "@/components/ui/card-grid";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  useImportSkillBulkPackage,
  useImportSkillPackage,
  useSkills,
  useValidateSkillBulkPackage,
  useValidateSkillPackage,
} from "@/hooks/api/useSkillApi";
import { SkillBulkImportDialog } from "@/components/ui/skill-bulk-import-dialog";
import { useEntityFilter } from "@/hooks/useEntityFilter";
import { useViewMode } from "@/hooks/useViewMode";
import { VisibilityEmptyHint } from "@/components/ui/visibility-filter";
import { EntityTable, EntityNameCell, type EntityTableColumn } from "@/components/ui/entity-table";
import { TagList } from "@/components/ui/tag-list";
import { EmptyState } from "@/components/ui/empty-state";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { Tip } from "@/components/ui/tooltip";
import { useAuth } from "@/hooks/useAuth";
import { cn, isEntityShared } from "@/lib/utils";
import { AgenticGateDialog, AgenticEEBadgeForButton } from "@/components/ui/license-gate-dialog";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { useCommandHandler } from "@/lib/commands/CommandsProvider";
import type {
  AgentSkill,
  BulkSkillCollisionPolicy,
  SkillBulkImportResponse,
  SkillBulkPackageValidationResponse,
} from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";

// ---------------------------------------------------------------------------
// Skill Card
// ---------------------------------------------------------------------------

function SkillCard({ skill, userId }: { skill: AgentSkill; userId?: string }) {
  const { t_i18n, intl } = useFormatter();
  const isShared = isEntityShared(skill, userId);
  return (
    <Link to={`/skills/${skill.id}`} className="block h-full">
      <Card
        className={cn(
          "h-full transition-all cursor-pointer hover:bg-elevation-hover",
          skill.enabled ? "border-elevation-subtle" : "border-elevation-subtle-soft opacity-60",
        )}
      >
        <CardContent className="p-4 flex flex-col h-full">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-accent/10">
              <GraduationCap className="h-5 w-5 text-accent" />
            </div>
            {/* The name keeps a readable minimum; the badge cluster wraps first. */}
            <div className="min-w-16 flex-1">
              {/* Both lines truncate in a narrow card: the full text is the hover title, as in EntityNameCell. */}
              <p className="content-base font-medium truncate" title={skill.name}>{skill.name}</p>
              <p className="content-compact text-default-secondary truncate" title={skill.description || undefined}>{skill.description || t_i18n("No description")}</p>
            </div>
            <div className="flex flex-wrap items-center justify-end gap-1">
              <Badge variant={skill.enabled ? "success" : "outline"} className="content-compact">
                {skill.enabled ? t_i18n("Enabled") : t_i18n("Disabled")}
              </Badge>
              {skill.is_company_managed && <CompanyManagedBadge label="Company" />}
              {isShared && <SharedBadge ownerName={skill.owner_name} />}
              {skill.source_integration_id && skill.auth_failure_count > 0 && (
                <Tip content={t_i18n("The GitHub credential attached to this skill is being rejected, so its content is no longer being updated. Re-authorize the integration.")}>
                  <Badge variant="outline" className="content-compact gap-1 text-feedback-error-primary border-feedback-error-primary/30">
                    <KeyRound className="h-3 w-3" />
                    {t_i18n("Credential")}
                  </Badge>
                </Tip>
              )}
            </div>
          </div>

          {/* Metadata row — pushed to bottom */}
          <div className="flex items-center gap-3 mt-auto pt-3 border-t border-elevation-subtle-soft">
            {skill.url ? (
              <>
                <Badge variant="outline" className="content-compact gap-1 shrink-0">
                  <LinkIcon className="h-3 w-3" /> {t_i18n("URL-based")}
                </Badge>
                {skill.fetched_at && (
                  <span className="content-compact text-default-secondary min-w-0 truncate">{t_i18n("Fetched {time}", { time: formatTimeAgo(intl, skill.fetched_at) })}</span>
                )}
              </>
            ) : (
              <Badge variant="outline" className="content-compact gap-1 shrink-0">
                <FileText className="h-3 w-3" /> {t_i18n("Direct content")}
              </Badge>
            )}
            {/* The compatibility badge gives up space before the tags do, down to
                a readable minimum: about five characters, the full text in its Tip. */}
            {skill.compatibility && (
              <Tip content={skill.compatibility}>
                <Badge variant="outline" className="content-compact gap-1 px-1.5 text-feedback-warning-primary border-feedback-alert-primary/30 min-w-12 shrink-[10]">
                  <span className="truncate">{skill.compatibility}</span>
                </Badge>
              </Tip>
            )}
            <TagList tags={skill.tags} className="ml-auto" />
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}

// ---------------------------------------------------------------------------
// Skills List View
// ---------------------------------------------------------------------------

/** List-view columns — name, source, status, compatibility, fetch time, tags. */
function useSkillColumns(userId?: string): EntityTableColumn<AgentSkill>[] {
  const { t_i18n, intl } = useFormatter();
  return useMemo(() => [
    {
      key: "name",
      header: "Name",
      sortValue: (s) => s.name,
      cell: (s) => (
        <EntityNameCell
          icon={<GraduationCap className="h-4 w-4 text-accent" />}
          name={s.name}
          description={s.description || null}
          muted={!s.enabled}
          badges={
            <>
              {s.is_company_managed && <CompanyManagedBadge />}
              {isEntityShared(s, userId) && <SharedBadge ownerName={s.owner_name} />}
              {s.source_integration_id && s.auth_failure_count > 0 && (
                <Tip content={t_i18n("The GitHub credential attached to this skill is being rejected, so its content is no longer being updated. Re-authorize the integration.")}>
                  <Badge variant="outline" className="content-compact gap-1 text-feedback-error-primary border-feedback-error-primary/30">
                    <KeyRound className="h-3 w-3" />
                    {t_i18n("Credential")}
                  </Badge>
                </Tip>
              )}
            </>
          }
        />
      ),
    },
    {
      key: "status",
      header: "Status",
      sortValue: (s) => s.enabled,
      cell: (s) => (
        <Badge variant={s.enabled ? "success" : "outline"} className="content-compact">
          {s.enabled ? t_i18n("Enabled") : t_i18n("Disabled")}
        </Badge>
      ),
    },
    {
      key: "source",
      header: "Source",
      // Source, Compatibility and Fetched show from 1280 px: below it the
      // table fits its card without a horizontal scroll (the skill page and
      // the card view still show them). hideBelow (a 56rem table), so the
      // Columns picker brings them back.
      hideBelow: "4xl",
      className: "whitespace-nowrap",
      sortValue: (s) => s.url ? "URL-based" : "Direct content",
      cell: (s) => (
        <Badge variant="outline" className="content-compact gap-1">
          {s.url
            ? <><LinkIcon className="h-3 w-3" /> {t_i18n("URL-based")}</>
            : <><FileText className="h-3 w-3" /> {t_i18n("Direct content")}</>}
        </Badge>
      ),
    },
    {
      key: "compatibility",
      header: "Compatibility",
      hideBelow: "4xl",
      sortValue: (s) => s.compatibility,
      cell: (s) => s.compatibility ? (
        <Tip content={s.compatibility}>
          <Badge variant="outline" className="content-compact text-feedback-warning-primary border-feedback-alert-primary/30 max-w-[160px]">
            <span className="truncate">{s.compatibility}</span>
          </Badge>
        </Tip>
      ) : (
        <span className="text-default-disabled">—</span>
      ),
    },
    {
      key: "tags",
      header: "Tags",
      // 120 px, the rest folded into "+N" (as on /agents): at its natural
      // width Tags pushed the table past its card (914 px in 778 at 1024).
      className: "w-[120px] max-w-[120px]",
      cell: (s) => <TagList tags={s.tags} />,
    },
    {
      key: "fetched",
      header: "Fetched",
      align: "right",
      hideBelow: "4xl",
      className: "whitespace-nowrap text-default-secondary",
      sortValue: (s) => s.fetched_at,
      cell: (s) => s.fetched_at ? formatTimeAgo(intl, s.fetched_at) : <span className="text-default-disabled">—</span>,
    },
    {
      key: "updated",
      header: "Updated",
      align: "right",
      className: "whitespace-nowrap text-default-secondary",
      sortValue: (s) => s.updated_at,
      cell: (s) => formatTimeAgo(intl, s.updated_at),
    },
  ], [t_i18n, intl, userId]);
}

// ---------------------------------------------------------------------------
// Skills List Page
// ---------------------------------------------------------------------------

export default function Skills() {
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();
  const { data: skills, isLoading } = useSkills();
  const importPackage = useImportSkillPackage();
  const validatePackage = useValidateSkillPackage();
  const validateBulkPackage = useValidateSkillBulkPackage();
  const importBulkPackage = useImportSkillBulkPackage();
  const { toast } = useToast();
  const { user } = useAuth();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const agenticAIEnabled = useAgenticAIEnabled();
  // Bulk review state: a ZIP is validated first, then approved skill by skill
  // in the review dialog.
  const [bulkFile, setBulkFile] = useState<File | null>(null);
  const [bulkValidation, setBulkValidation] = useState<SkillBulkPackageValidationResponse | null>(null);
  const [bulkResult, setBulkResult] = useState<SkillBulkImportResponse | null>(null);
  const [bulkOpen, setBulkOpen] = useState(false);
  /* Declared in `Skills.commands.ts` — ⌘K > "Skills: Import a skill package". */
  useCommandHandler("skills.importPackage", () => setBulkOpen(true));
  const [viewMode, setViewMode] = useViewMode("skills");
  const columns = useSkillColumns(user?.id);
  const { search, setSearch, selectedTags, setSelectedTags, visibilityFilter, setVisibilityFilter, visibilityCounts, clearAll, allItems: allSkills, allTags, filtered } =
    useEntityFilter({ items: skills, userId: user?.id });

  const summarizeMappingWarning = (warnings: string[]) => warnings.find((warning) =>
    warning.startsWith("Mapping manifest resolved:"));

  const handleImportPackage = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const lower = file.name.toLowerCase();
    const isSupported = lower.endsWith(".zip")
      || lower.endsWith(".md")
      || lower.endsWith(".markdown")
      || lower.endsWith(".txt");
    if (!isSupported) {
      toast({ title: t_i18n("Invalid file"), description: t_i18n("Please select a .zip package or a SKILL.md/.txt file."), variant: "error" });
      event.target.value = "";
      return;
    }
    // A ZIP may hold several skills, so it always goes through the review
    // dialog — including a single-skill package, which shows as one row.
    if (lower.endsWith(".zip")) {
      try {
        const validation = await validateBulkPackage.mutateAsync(file);
        if (!validation.valid) {
          throw new Error(validation.errors.join(" ") || "Package validation failed");
        }
        setBulkFile(file);
        setBulkValidation(validation);
        setBulkResult(null);
        setBulkOpen(true);
      } catch (err) {
        const message = err instanceof Error ? err.message : t_i18n("Import failed");
        toast({ title: t_i18n("Import failed"), description: message, variant: "error" });
      } finally {
        event.target.value = "";
      }
      return;
    }
    try {
      const validation = await validatePackage.mutateAsync(file);
      if (!validation.valid) {
        throw new Error(validation.errors.join(" ") || "Package validation failed");
      }
      await importPackage.mutateAsync({ file, onCollision: "cancel" });
      const mappingSummary = summarizeMappingWarning(validation.warnings);
      const otherWarnings = validation.warnings.filter((warning) => warning !== mappingSummary);
      const importedName = validation.detected_name ?? "skill";
      const importSummary = t_i18n("Imported {name} ({versions} versions, {resources} resources).", { name: importedName, versions: validation.version_count, resources: validation.resource_count });
      toast({
        title: t_i18n("Skill package imported"),
        description: mappingSummary ? `${importSummary} ${mappingSummary}` : importSummary,
        variant: "success",
      });
      if (otherWarnings.length > 0) {
        const [firstWarning] = otherWarnings;
        toast({
          title: t_i18n("Package warnings"),
          description: otherWarnings.length > 1 ? `${firstWarning} (+${otherWarnings.length - 1} more)` : firstWarning,
          variant: "info",
        });
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : t_i18n("Import failed");
      toast({ title: t_i18n("Import failed"), description: message, variant: "error" });
    } finally {
      event.target.value = "";
    }
  };

  const handleBulkConfirm = async (
    selectedPaths: string[],
    collisionOverrides: Record<string, BulkSkillCollisionPolicy>,
  ) => {
    if (!bulkFile) return;
    try {
      const result = await importBulkPackage.mutateAsync({
        file: bulkFile,
        selectedPaths,
        onCollision: "skip",
        collisionOverrides,
      });
      setBulkResult(result);
      const summary = t_i18n(
        "{created} created, {replaced} replaced, {skipped} skipped, {failed} failed.",
        {
          created: result.created_count,
          replaced: result.replaced_count,
          skipped: result.skipped_count,
          failed: result.failed_count,
        },
      );
      toast({
        title: result.failed_count > 0 ? t_i18n("Bulk import finished with errors") : t_i18n("Skills imported"),
        description: summary,
        variant: result.failed_count > 0 ? "info" : "success",
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : t_i18n("Import failed");
      toast({ title: t_i18n("Import failed"), description: message, variant: "error" });
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold">{t_i18n("Skills")}</h1>
          <p className="mt-2 text-default-secondary">
            {t_i18n("Shared instruction sets following the")}{" "}
            <a href="https://agentskills.io" target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">{t_i18n("Agent Skills spec")}</a>
            {" "}{t_i18n(": attach to any agent.")}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <input
            ref={fileInputRef}
            type="file"
            accept=".zip,.md,.markdown,.txt"
            onChange={handleImportPackage}
            className="hidden"
          />
          <AgenticGateDialog feature={t_i18n("Importing skill packages")}>
            <Button
              variant="outline"
              className="gap-1.5"
              onClick={() => fileInputRef.current?.click()}
              disabled={
                importPackage.isPending
                || validatePackage.isPending
                || validateBulkPackage.isPending
                || importBulkPackage.isPending
              }
            >
              <Upload className="h-4 w-4" /> {t_i18n("Import Package")}
              <AgenticEEBadgeForButton />
            </Button>
          </AgenticGateDialog>
          <AgenticGateDialog feature={t_i18n("Adding skills")}>
            <Button className="gap-1.5" onClick={() => navigate("/skills/new")}>
              <Plus className="h-4 w-4" /> {t_i18n("Add Skill")}
              <AgenticEEBadgeForButton />
            </Button>
          </AgenticGateDialog>
        </div>
      </div>

      {/* Skill grid / list */}
      {!isLoading && allSkills.length === 0 ? (
        <EmptyState
          icon={GraduationCap}
          title="No skills yet"
          requiresAi
          description="Create your first skill by URL, paste, or file upload to start enriching your agents."
          actionLabel={agenticAIEnabled ? "Add Skill" : undefined}
          onAction={agenticAIEnabled ? () => navigate("/skills/new") : undefined}
        />
      ) : (
        // Standard v2: the shared toolbar of the records table (search, scope, tags,
        // Clear all | total, Columns, page size, view), in list and grid.
        <EntityTable
          isLoading={isLoading}
          items={filtered}
          columns={columns}
          rowKey={(s) => s.id}
          rowHref={(s) => `/skills/${s.id}`}
          rowClassName={(s) => s.enabled ? undefined : "opacity-60"}
          defaultSortKey="name"
          storageKey="skills"
          search={{ value: search, onChange: setSearch, placeholder: t_i18n("Search skills...") }}
          scope={{ value: visibilityFilter, onChange: setVisibilityFilter, counts: visibilityCounts }}
          tags={{ all: allTags, selected: selectedTags, onChange: setSelectedTags }}
          view={{ value: viewMode, onChange: setViewMode }}
          // The first read draws this page's own card: no tag row, its footer at its own height (#11345).
          renderGridSkeleton={(pageSize) => <CardGridSkeleton count={cardSkeletonCount(pageSize)} label={t_i18n("Loading")} tags={false} meta="control" />}
          renderGrid={
            viewMode === "grid"
              ? (page) => (
                  <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                    {page.map((skill) => <SkillCard key={skill.id} skill={skill} userId={user?.id} />)}
                  </div>
                )
              : undefined
          }
          totalAll={allSkills.length}
          externalFilterCount={selectedTags.length}
          onClearAll={() => clearAll(undefined, { keepScope: true })}
          noResultsLabel={t_i18n("No skills match your filter.")}
          emptyContent={
            <VisibilityEmptyHint value={visibilityFilter} counts={visibilityCounts} onShowAll={() => setVisibilityFilter("all")}>
              <p className="content-base text-default-secondary text-center py-8">{t_i18n("No skills match your filter.")}</p>
            </VisibilityEmptyHint>
          }
        />
      )}

      <SkillBulkImportDialog
        open={bulkOpen}
        onOpenChange={(open) => {
          setBulkOpen(open);
          if (!open) {
            setBulkFile(null);
            setBulkValidation(null);
            setBulkResult(null);
          }
        }}
        fileName={bulkFile?.name ?? ""}
        validation={bulkValidation}
        result={bulkResult}
        isImporting={importBulkPackage.isPending}
        onConfirm={handleBulkConfirm}
      />
    </div>
  );
}
