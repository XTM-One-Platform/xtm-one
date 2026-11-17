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
 * What a workspace gives its agents: instructions, sources, bound
 * knowledge bases, linked skills and its conversation search. Shown on the
 * project page and in the knowledge drawer of any filed conversation.
 *
 * Every control follows the workspace's own right (`can_manage` of the
 * summary): a reader sees the same items without a way to change them. The
 * sources are the workspace's Sources knowledge base, managed with the
 * knowledge base's own tabs - its formats, limits and sync lifecycle - under
 * the workspace's right; it is created on the first source, never on a visit.
 */
import { useEffect, useState, type ReactNode } from "react";
import { BookOpen, Database, FileText, MessagesSquare, Plus, ScrollText, Sparkles } from "lucide-react";
import { Textarea, Spinner } from "@filigran/design-system";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { MultiSelect } from "@/components/ui/multi-select";
import { useToast } from "@/components/ui/toast";
import { WorkspaceKnowledgeBaseDialog } from "@/components/workspaces/WorkspaceKnowledgeBaseDialog";
import { useKnowledgeBase } from "@/hooks/api/useKnowledgeBaseApi";
import { useSkills } from "@/hooks/api/useSkillApi";
import {
  useEnsureWorkspaceSources,
  useSetWorkspaceSkills,
  useUpdateWorkspace,
  useWorkspace,
  useWorkspaceKnowledge,
  useWorkspaceKnowledgeBases,
  useWorkspaceSkills,
} from "@/hooks/api/useWorkspaceApi";
import { useFormatter } from "@/i18n";
import { DocumentsTab } from "@/pages/knowledge-base-detail/DocumentsTab";
import { SourcesTab } from "@/pages/knowledge-base-detail/SourcesTab";
import type { Workspace, WorkspaceKnowledgeSummary } from "@/types";

/** Mirrors `WorkspaceCreate.instructions` (`schemas/workspace.py`). */
const INSTRUCTIONS_MAX = 8000;

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

export function KnowledgePanel({ workspaceId }: { workspaceId: string }) {
  const { t_i18n } = useFormatter();
  const workspace = useWorkspace(workspaceId);
  const summary = useWorkspaceKnowledge(workspaceId);

  if (workspace.isLoading || summary.isLoading) {
    return (
      <div className="flex items-center gap-2 py-6 content-compact text-default-secondary">
        <Spinner size="sm" tone="inherit" className="text-accent" />
        {t_i18n("Loading…")}
      </div>
    );
  }
  if (!workspace.data || !summary.data) {
    return (
      <p className="py-6 content-compact text-feedback-error-primary">
        {t_i18n("The workspace's knowledge could not be loaded.")}
      </p>
    );
  }
  return (
    <div className="space-y-4">
      <InstructionsCard workspace={workspace.data} summary={summary.data} />
      <SourcesCard workspace={workspace.data} summary={summary.data} />
      <KnowledgeBasesCard workspace={workspace.data} canManage={summary.data.can_manage} />
      <SkillsCard workspaceId={workspaceId} canManage={summary.data.can_manage} />
      <ConversationSearchCard summary={summary.data} />
    </div>
  );
}

function SectionTitle({ icon: Icon, children }: { icon: typeof BookOpen; children: ReactNode }) {
  return (
    <CardTitle className="flex items-center gap-2 content-base">
      <Icon className="h-4 w-4 text-accent" />
      {children}
    </CardTitle>
  );
}

function InstructionsCard({ workspace, summary }: { workspace: Workspace; summary: WorkspaceKnowledgeSummary }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateWorkspace();
  const [draft, setDraft] = useState<string | null>(null);
  const value = draft ?? summary.instructions;
  const changed = draft !== null && draft !== summary.instructions;
  // The saved text stays on screen until the refetched summary carries it.
  useEffect(() => {
    if (draft !== null && draft === summary.instructions) setDraft(null);
  }, [draft, summary.instructions]);

  const save = async () => {
    try {
      await update.mutateAsync({ id: workspace.id, instructions: value });
      toast({ title: t_i18n("Instructions saved"), variant: "success" });
    } catch (err) {
      toast({
        title: t_i18n("The instructions could not be saved"),
        description: errorText(err, t_i18n("Unknown error")),
        variant: "error",
      });
    }
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <SectionTitle icon={ScrollText}>{t_i18n("Instructions")}</SectionTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <p className="content-caption text-default-secondary">
          {t_i18n("Every agent working in a conversation of this workspace receives them.")}
        </p>
        {summary.can_manage ? (
          <>
            <Textarea
              aria-label={t_i18n("Instructions")}
              placeholder={t_i18n("How agents should work in this workspace: context, conventions, what to avoid…")}
              value={value}
              maxLength={INSTRUCTIONS_MAX}
              rows={6}
              onChange={(e) => setDraft(e.target.value)}
            />
            <div className="flex items-center justify-between">
              <span className="content-caption text-default-secondary">
                {t_i18n("{count} / {max} characters", { count: value.length, max: INSTRUCTIONS_MAX })}
              </span>
              <div className="flex gap-2">
                {changed && (
                  <Button variant="ghost" size="sm" onClick={() => setDraft(null)}>
                    {t_i18n("Cancel")}
                  </Button>
                )}
                <Button size="sm" disabled={!changed || update.isPending} onClick={save}>
                  {t_i18n("Save")}
                </Button>
              </div>
            </div>
          </>
        ) : summary.instructions ? (
          <p className="whitespace-pre-wrap content-compact text-default-primary">{summary.instructions}</p>
        ) : (
          <p className="content-compact text-default-secondary">{t_i18n("No instructions.")}</p>
        )}
      </CardContent>
    </Card>
  );
}

function SourcesCard({ workspace, summary }: { workspace: Workspace; summary: WorkspaceKnowledgeSummary }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const ensure = useEnsureWorkspaceSources();
  const [open, setOpen] = useState(false);
  const kbId = summary.sources_knowledge_base_id;
  const counts = summary.sources;

  const start = async () => {
    try {
      await ensure.mutateAsync(workspace.id);
      setOpen(true);
    } catch (err) {
      toast({
        title: t_i18n("The sources could not be opened"),
        description: errorText(err, t_i18n("Unknown error")),
        variant: "error",
      });
    }
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <SectionTitle icon={FileText}>{t_i18n("Sources")}</SectionTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="content-caption text-default-secondary">
          {t_i18n(
            "Documents every agent of this workspace works with: given whole while they fit, searched beyond.",
          )}
        </p>
        {counts.count > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {counts.ready > 0 && <Badge variant="success">{t_i18n("{count} ready", { count: counts.ready })}</Badge>}
            {counts.pending > 0 && <Badge variant="outline">{t_i18n("{count} processing", { count: counts.pending })}</Badge>}
            {counts.failed > 0 && <Badge variant="destructive">{t_i18n("{count} failed", { count: counts.failed })}</Badge>}
          </div>
        )}
        {kbId ? (
          <>
            <Button variant="secondary" size="sm" className="gap-1.5" onClick={() => setOpen(true)}>
              <FileText className="h-3.5 w-3.5" />
              {summary.can_manage ? t_i18n("Manage sources") : t_i18n("View sources")}
            </Button>
            {open && (
              <SourcesDialog
                workspaceName={workspace.name}
                kbId={kbId}
                canManage={summary.can_manage}
                onClose={() => setOpen(false)}
              />
            )}
          </>
        ) : summary.can_manage ? (
          <Button variant="secondary" size="sm" className="gap-1.5" disabled={ensure.isPending} onClick={start}>
            <Plus className="h-3.5 w-3.5" />
            {t_i18n("Add sources")}
          </Button>
        ) : (
          <p className="content-compact text-default-secondary">{t_i18n("No sources yet.")}</p>
        )}
      </CardContent>
    </Card>
  );
}

/** The Sources knowledge base's own tabs - documents, then synchronised
 *  sources - at a width they were laid out for. */
function SourcesDialog({
  workspaceName,
  kbId,
  canManage,
  onClose,
}: {
  workspaceName: string;
  kbId: string;
  canManage: boolean;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const kb = useKnowledgeBase(kbId);
  const [tab, setTab] = useState<"documents" | "sync">("documents");
  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-h-[85vh] max-w-4xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{t_i18n("Sources of {name}", { name: workspaceName })}</DialogTitle>
          <DialogDescription>
            {t_i18n("Uploaded files, pasted text and synchronised sources every agent of this workspace works with.")}
          </DialogDescription>
        </DialogHeader>
        <SegmentedFilter
          value={tab}
          onChange={setTab}
          ariaLabel={t_i18n("Sources")}
          className="self-start"
          options={[
            { key: "documents", label: t_i18n("Documents") },
            { key: "sync", label: t_i18n("Synchronised sources") },
          ]}
        />
        {kb.data &&
          (tab === "documents" ? (
            <DocumentsTab kb={kb.data} canManage={canManage} />
          ) : (
            <SourcesTab kb={kb.data} canManage={canManage} />
          ))}
      </DialogContent>
    </Dialog>
  );
}

function KnowledgeBasesCard({ workspace, canManage }: { workspace: Workspace; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const bound = useWorkspaceKnowledgeBases(workspace.id);
  const [editing, setEditing] = useState(false);
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <SectionTitle icon={Database}>{t_i18n("Knowledge bases")}</SectionTitle>
        {canManage && (
          <Button variant="ghost" size="sm" onClick={() => setEditing(true)}>
            {t_i18n("Manage")}
          </Button>
        )}
      </CardHeader>
      <CardContent className="space-y-1">
        {(bound.data ?? []).length === 0 ? (
          <p className="content-compact text-default-secondary">{t_i18n("No knowledge base bound.")}</p>
        ) : (
          (bound.data ?? []).map((kb) => (
            <p key={kb.knowledge_base_id} className="truncate content-compact text-default-primary">
              {kb.name}
            </p>
          ))
        )}
      </CardContent>
      {editing && <WorkspaceKnowledgeBaseDialog workspace={workspace} onClose={() => setEditing(false)} />}
    </Card>
  );
}

function SkillsCard({ workspaceId, canManage }: { workspaceId: string; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const linked = useWorkspaceSkills(workspaceId);
  // The library is the picker's: a reader never sees it.
  const skills = useSkills({ enabled: canManage });
  const save = useSetWorkspaceSkills();
  // While a change is saved, it is the set shown; a second toggle waits for it,
  // because the endpoint replaces the whole set and the two would race.
  const current = save.isPending && save.variables
    ? save.variables.skillIds
    : (linked.data ?? []).map((s) => s.skill_id);

  const toggle = async (skillId: string) => {
    // The endpoint replaces the set: never send one built before it was known.
    if (!linked.isSuccess || save.isPending) return;
    const next = current.includes(skillId) ? current.filter((id) => id !== skillId) : [...current, skillId];
    try {
      await save.mutateAsync({ id: workspaceId, skillIds: next });
    } catch (err) {
      toast({
        title: t_i18n("The skills could not be saved"),
        description: errorText(err, t_i18n("Unknown error")),
        variant: "error",
      });
    }
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <SectionTitle icon={Sparkles}>{t_i18n("Skills")}</SectionTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <p className="content-caption text-default-secondary">
          {t_i18n("Always in the catalogue of every agent working here; loaded when needed.")}
        </p>
        {canManage ? (
          <MultiSelect
            options={(skills.data ?? []).map((s) => ({ value: s.id, label: s.name }))}
            selected={current}
            onToggle={(value) => void toggle(value)}
            placeholder={t_i18n("Select skills")}
            searchPlaceholder={t_i18n("Search skills...")}
            emptyOptionsLabel={t_i18n("No skills yet")}
            noMatchLabel={t_i18n("No match")}
          />
        ) : (linked.data ?? []).length === 0 ? (
          <p className="content-compact text-default-secondary">{t_i18n("No skill linked.")}</p>
        ) : (
          (linked.data ?? []).map((s) => (
            <p key={s.skill_id} className="truncate content-compact text-default-primary">
              {s.name}
            </p>
          ))
        )}
      </CardContent>
    </Card>
  );
}

function ConversationSearchCard({ summary }: { summary: WorkspaceKnowledgeSummary }) {
  const { t_i18n } = useFormatter();
  const { indexed, pending } = summary.conversations;
  return (
    <Card>
      <CardHeader className="pb-2">
        <SectionTitle icon={MessagesSquare}>{t_i18n("Conversation search")}</SectionTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <p className="content-caption text-default-secondary">
          {t_i18n(
            "Agents here can search the conversations filed in this workspace, once each has been quiet for a while. Tool results are never indexed.",
          )}
        </p>
        <div className="flex flex-wrap gap-1.5">
          <Badge variant="cyan">{t_i18n("{count} searchable", { count: indexed })}</Badge>
          {pending > 0 && <Badge variant="outline">{t_i18n("{count} waiting", { count: pending })}</Badge>}
        </div>
      </CardContent>
    </Card>
  );
}
