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

import type { Dispatch, ReactNode, SetStateAction } from "react";
import {
  BookOpen, Brain, GraduationCap, Pencil,
  Building2, CheckCircle2, Plus, Lock,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { CardEditActions } from "@/components/ui/card-edit-actions";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { SearchInput } from "@/components/ui/search-input";
import { PromptsCard } from "./PromptsCard";
import { SkillsCard } from "./SkillsCard";
import { useSkillPackageBindings } from "@/hooks/api/useSkillApi";
import {
  packageCarrierHint, packageOwnerHint, packageResourceCarriers, packageResourceOwners,
} from "@/lib/skillPackage";
import { cn, formatKBStats } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { Agent, KnowledgeBase } from "@/types";

/** Agent fields backing the four independently toggleable memory tiers. */
export type MemoryTierField =
  | "memory_short_term_enabled"
  | "memory_medium_term_enabled"
  | "memory_persistent_enabled"
  | "memory_user_enabled";

const MEMORY_TIERS: {
  field: MemoryTierField;
  label: string;
  /** Takes the formatter so the persistent tier can inject a rich fragment
   *  (the `search_memory` tool name) into its translated sentence. */
  description: (fmt: ReturnType<typeof useFormatter>) => ReactNode;
}[] = [
  {
    field: "memory_short_term_enabled",
    label: "Short-term Memory",
    description: ({ t_i18n }) => t_i18n("Recent assignment results and chat exchanges retained for 24 hours (rolling 10 entries), giving the agent immediate context."),
  },
  {
    field: "memory_medium_term_enabled",
    label: "Medium-term Memory",
    description: ({ t_i18n }) => t_i18n("Periodically consolidates short-term observations into a durable summary of patterns, preferences, and key knowledge (retained for 30 days)."),
  },
  {
    field: "memory_persistent_enabled",
    label: "Persistent Memory",
    description: ({ intl }) => intl.formatMessage(
      { id: "Key facts and knowledge are extracted and stored durably with semantic search. Survives indefinitely and can be browsed in the Memory tab. Adds a {tool} tool to the agent." },
      { tool: <span className="font-medium text-default-primary">search_memory</span> },
    ),
  },
  {
    field: "memory_user_enabled",
    label: "User Memory",
    description: ({ t_i18n }) => t_i18n("Cross-agent personalization that remembers user preferences and context across different agents. Disable for fully stateless chat sessions."),
  },
];

type KnowledgeSectionProps = {
  agent: Agent;
  canManage: boolean;
  isPending: boolean;
  companyManaged: boolean;
  // Prompts
  promptIds: string[];
  setPromptIds: Dispatch<SetStateAction<string[]>>;
  editingPrompts: boolean;
  onStartEditPrompts: () => void;
  onSavePrompts: () => void;
  onCancelPrompts: () => void;
  // Skills
  skillIds: string[];
  setSkillIds: Dispatch<SetStateAction<string[]>>;
  skillLoadModes: Record<string, "progressive" | "always">;
  setSkillLoadModes: Dispatch<SetStateAction<Record<string, "progressive" | "always">>>;
  editingSkills: boolean;
  onStartEditSkills: () => void;
  onSaveSkills: () => void;
  onCancelSkills: () => void;
  // Knowledge Bases
  editingKbs: boolean;
  kbIds: string[];
  onToggleKb: (id: string) => void;
  kbPermissions: Record<string, "read" | "write">;
  onSetKbPermission: (id: string, perm: "read" | "write") => void;
  kbSearch: string;
  setKbSearch: (v: string) => void;
  availableKbs: KnowledgeBase[];
  kbs: KnowledgeBase[] | undefined;
  onStartEditKbs: () => void;
  onSaveKbs: () => void;
  onCancelKbs: () => void;
  // Memory
  memoryShortTerm: boolean;
  memoryMediumTerm: boolean;
  memoryPersistent: boolean;
  memoryUser: boolean;
  onSaveMemoryTier: (tier: MemoryTierField, val: boolean) => void;
};

export function KnowledgeSection({
  agent,
  canManage,
  isPending,
  companyManaged,
  promptIds,
  setPromptIds,
  editingPrompts,
  onStartEditPrompts,
  onSavePrompts,
  onCancelPrompts,
  skillIds,
  setSkillIds,
  skillLoadModes,
  setSkillLoadModes,
  editingSkills,
  onStartEditSkills,
  onSaveSkills,
  onCancelSkills,
  editingKbs,
  kbIds,
  onToggleKb,
  kbPermissions,
  onSetKbPermission,
  kbSearch,
  setKbSearch,
  availableKbs,
  kbs,
  onStartEditKbs,
  onSaveKbs,
  onCancelKbs,
  memoryShortTerm,
  memoryMediumTerm,
  memoryPersistent,
  memoryUser,
  onSaveMemoryTier,
}: KnowledgeSectionProps) {
  const formatter = useFormatter();
  const { t_i18n, intl } = formatter;
  const memoryDraft: Record<MemoryTierField, boolean> = {
    memory_short_term_enabled: memoryShortTerm,
    memory_medium_term_enabled: memoryMediumTerm,
    memory_persistent_enabled: memoryPersistent,
    memory_user_enabled: memoryUser,
  };
  // Why the memory tiers are locked. Built-ins are rejected outright by the
  // backend, company-managed agents are admin-only, and the remaining case is a
  // read-only group member on someone else's agent.
  const memoryLockReason = agent.is_builtin
    ? t_i18n("Configured by the platform: memory tiers of built-in agents cannot be modified.")
    : agent.is_company_managed
      ? t_i18n("This is a company-managed agent: only admins can change its memory tiers.")
      : t_i18n("You don't have permission to change the memory tiers of this agent.");

  // KBs produced by a packaged skill are owned by that skill: the backend keeps
  // them in sync with the skill on every save, so neither detaching one that is
  // attached nor attaching one whose skill is missing would survive — both are
  // presented as skill-driven instead of offering a toggle that gets undone.
  const { data: packageBindings } = useSkillPackageBindings();
  const kbOwners = packageResourceOwners(packageBindings, agent.skill_ids).knowledgeBases;
  const kbCarriers = packageResourceCarriers(packageBindings).knowledgeBases;

  return (
    <>
      {/* Prompts */}
      <PromptsCard
        agent={agent}
        promptIds={promptIds}
        setPromptIds={setPromptIds}
        editing={editingPrompts}
        canManage={canManage}
        onStartEdit={onStartEditPrompts}
        onSave={onSavePrompts}
        onCancel={onCancelPrompts}
        saving={isPending}
        companyManaged={companyManaged}
      />

      {/* Skills */}
      <SkillsCard
        agent={agent}
        skillIds={skillIds}
        setSkillIds={setSkillIds}
        skillLoadModes={skillLoadModes}
        setSkillLoadModes={setSkillLoadModes}
        editing={editingSkills}
        canManage={canManage}
        onStartEdit={onStartEditSkills}
        onSave={onSaveSkills}
        onCancel={onCancelSkills}
        saving={isPending}
        companyManaged={companyManaged}
      />

      {/* Knowledge bases */}
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
          <div>
            <CardTitle className="content-base flex items-center gap-2">
              <BookOpen className="h-4 w-4 text-accent" /> {t_i18n("Knowledge Bases")}
              {(agent.knowledge_base_ids || []).length > 0 && !editingKbs && (
                <CountBadge variant="secondary">{(agent.knowledge_base_ids || []).length}</CountBadge>
              )}
            </CardTitle>
            {companyManaged && <p className="content-caption text-feedback-warning-primary mt-0.5 flex items-center gap-1"><Building2 className="h-3 w-3" /> {t_i18n("Only company-managed knowledge bases are shown.")}</p>}
          </div>
          <CardEditActions editing={editingKbs} canEdit={canManage} onEdit={onStartEditKbs} onCancel={onCancelKbs} onSave={onSaveKbs} saveDisabled={isPending} />
        </CardHeader>
        <CardContent className="px-4 pb-4 pt-0">
          {editingKbs ? (
            <>
              <p className="content-caption text-default-secondary mb-3">{intl.formatMessage(
                { id: "Click to attach or detach knowledge bases. Attached KBs are {searchable} (read) by this agent in chat and assignments. Enable {write} per KB below to let the agent save documents into it via {tool}." },
                {
                  searchable: <span className="text-default-primary">{t_i18n("searchable")}</span>,
                  write: <span className="text-default-primary">{t_i18n("write")}</span>,
                  tool: <span className="font-medium">store_knowledge</span>,
                },
              )}</p>
              {(kbs ?? []).filter((kb) => !companyManaged || kb.is_company_managed).length > 3 && (
                <SearchInput placeholder={t_i18n("Filter knowledge bases...")} value={kbSearch} onChange={setKbSearch} className="mb-3 max-w-full" />
              )}
              {availableKbs.length > 0 ? (
                <div className="grid gap-2 sm:grid-cols-3">
                  {availableKbs.map((kb) => {
                    const isAttached = kbIds.includes(kb.id);
                    const isSystemManaged = kb.is_system_managed;
                    // Only a KB that IS attached can be skill-owned: a
                    // company-managed agent skips a personal package KB, and
                    // labelling that one "via skill" would be a lie.
                    const skillOwners = isAttached ? kbOwners[kb.id] : undefined;
                    const isSkillOwned = !!skillOwners?.length;
                    // Packaged, but no attached skill carries it — whether it is
                    // currently attached or not. Either way the toggle is dead:
                    // the reconciliation adds it only with its skill and strips it
                    // without, so point at the skill instead. Covers the legacy /
                    // hand-attached state where the KB is bound but the skill is not.
                    const skillCarriers = isSkillOwned ? undefined : kbCarriers[kb.id];
                    const needsItsSkill = !!skillCarriers?.length;
                    const isLocked = (isSystemManaged && isAttached) || isSkillOwned || needsItsSkill;
                    return (
                      <Tip
                        key={kb.id}
                        content={
                          packageOwnerHint(skillOwners, t_i18n)
                          ?? packageCarrierHint(skillCarriers, t_i18n)
                          ?? (isLocked ? t_i18n("System-managed KB required by assignments: cannot be unbound") : undefined)
                        }
                      >
                      <button
                        className={cn(
                          "flex items-center gap-3 rounded-sm border p-3 text-left transition-all",
                          isLocked && "cursor-not-allowed opacity-75",
                          isAttached ? "border-accent/40 bg-accent/5" : "border-elevation-subtle-soft bg-elevation-hover hover:border-elevation-subtle",
                        )}
                        onClick={() => { if (!isLocked) onToggleKb(kb.id); }}
                      >
                        <BookOpen className={cn("h-5 w-5 shrink-0", isAttached ? "text-accent" : "text-default-secondary")} />
                        <div className="min-w-0 flex-1">
                          <p className={cn("content-base font-medium truncate", isAttached && "text-accent")}>{kb.name}</p>
                          <div className="flex items-center gap-1.5 mt-0.5">
                            <span className="content-caption text-default-secondary">{formatKBStats(kb.document_count, kb.chunk_count)}</span>
                            {isSystemManaged && <Badge variant="cyan" className="text-[7px]">{t_i18n("System")}</Badge>}
                            {(isSkillOwned || needsItsSkill) && <Badge variant="secondary" className="text-[7px]">{t_i18n("Skill")}</Badge>}
                          </div>
                        </div>
                        {isSkillOwned ? (
                          <span className="text-[8px] text-accent shrink-0">{t_i18n("Via skill")}</span>
                        ) : needsItsSkill ? (
                          <span className="text-[8px] text-default-secondary shrink-0">{t_i18n("Needs skill")}</span>
                        ) : isLocked ? (
                          <span className="text-[8px] text-accent shrink-0">{t_i18n("Required")}</span>
                        ) : isAttached ? (
                          <CheckCircle2 className="h-4 w-4 text-accent shrink-0" />
                        ) : (
                          <Plus className="h-4 w-4 text-default-disabled shrink-0" />
                        )}
                      </button>
                      </Tip>
                    );
                  })}
                </div>
              ) : (
                <div className="py-4 text-center">
                  <BookOpen className="h-6 w-6 text-default-disabled mx-auto mb-1.5" />
                  <p className="content-base text-default-secondary">{t_i18n("No knowledge bases")}</p>
                  <p className="content-caption text-default-secondary mt-0.5">{t_i18n("Create them in the Knowledge page.")}</p>
                </div>
              )}

              {/* Per-KB write access */}
              {kbIds.length > 0 && (
                <div className="mt-4 border-t border-elevation-subtle-soft pt-4 space-y-2">
                  <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Access: enable writing per knowledge base")}</p>
                  {kbIds
                    .map((id) => ({ id, kb: kbs?.find((k) => k.id === id) }))
                    .filter((x): x is { id: string; kb: NonNullable<typeof x.kb> } => !!x.kb)
                    .sort((a, b) => a.kb.name.localeCompare(b.kb.name))
                    .map(({ id, kb }) => {
                      // A content-locked knowledge base (read-only, installed by a
                      // skill package, built-in) can never be written to, whatever
                      // this grant says — the server strips it from the agent's
                      // write scope. Offering a toggle that silently does nothing
                      // would be a lie, so lock it here too, from the server's
                      // own answer.
                      const lock = kb.content_lock ?? null;
                      const locked = lock !== null;
                      const canWrite = !locked && kbPermissions[id] === "write";
                      const lockNotice =
                        lock === "package_installed"
                          ? t_i18n("This {entity} was installed by a skill package and cannot be modified here. Update or remove the skill package to change it.", { entity: t_i18n("knowledge base") })
                          : lock === "builtin"
                            ? t_i18n("This is a built-in {entity} pre-configured by the platform. It cannot be modified.", { entity: t_i18n("knowledge base") })
                            : t_i18n("This knowledge base is locked as ground truth. Turn off read-only on the knowledge base to allow writes.");
                      return (
                        <div key={id} className="flex items-center justify-between gap-3 rounded-sm bg-elevation-hover px-3 py-2">
                          <div className="min-w-0 flex-1">
                            <p className="content-compact font-medium truncate">{kb.name}</p>
                            <p className="content-caption text-default-secondary">
                              {locked
                                ? t_i18n("Read-only knowledge base: write access cannot be granted")
                                : canWrite
                                  ? t_i18n("Read & write (store_knowledge enabled)")
                                  : t_i18n("Read-only (search only)")}
                            </p>
                          </div>
                          {locked ? (
                            <Tip content={lockNotice}>
                              <Lock className="h-3.5 w-3.5 text-feedback-warning-primary" />
                            </Tip>
                          ) : (
                            <Switch aria-label={kb.name} checked={canWrite} onCheckedChange={(v) => onSetKbPermission(id, v ? "write" : "read")} />
                          )}
                        </div>
                      );
                    })}
                </div>
              )}
            </>
          ) : (
            <div>
              {(agent.knowledge_base_ids || []).length > 0 ? (
                <div className="space-y-1.5">
                  {(agent.knowledge_base_ids || [])
                    .map((kbId) => ({ kbId, kb: kbs?.find((k) => k.id === kbId) }))
                    .filter((x): x is { kbId: string; kb: NonNullable<typeof x.kb> } => !!x.kb)
                    .sort((a, b) => a.kb.name.localeCompare(b.kb.name))
                    .map(({ kbId, kb }) => (
                      <div key={kbId} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5">
                        <BookOpen className="h-4 w-4 text-accent shrink-0" />
                        <div className="min-w-0 flex-1">
                          <p className="content-base font-medium">{kb.name}</p>
                          <p className="content-caption text-default-secondary">{formatKBStats(kb.document_count, kb.chunk_count)}</p>
                        </div>
                        {(agent.knowledge_base_permissions?.[kbId] === "write") && (
                          <Badge variant="secondary" className="text-[7px] gap-1"><Pencil className="h-2.5 w-2.5" /> {t_i18n("Write")}</Badge>
                        )}
                        {!!kbOwners[kbId]?.length && (
                          <Tip content={packageOwnerHint(kbOwners[kbId], t_i18n)}>
                            <Badge variant="secondary" className="text-[7px] gap-1"><GraduationCap className="h-2.5 w-2.5" /> {kbOwners[kbId].join(", ")}</Badge>
                          </Tip>
                        )}
                        {kb.is_system_managed && <Badge variant="cyan" className="text-[7px]">{t_i18n("System")}</Badge>}
                      </div>
                    ))}
                </div>
              ) : (
                <p className="content-base text-default-secondary italic py-3 text-center">{t_i18n("No knowledge bases attached")}</p>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Agent Memory */}
      <Card>
        <CardHeader>
          <CardTitle className="content-base flex items-center gap-2">
            <Brain className="h-4 w-4 text-accent" /> {t_i18n("Agent Memory")}
            {!canManage && <ReadOnlyBadge size="sm" />}
          </CardTitle>
          <p className="content-caption text-default-secondary mt-1">
            {canManage
              ? t_i18n("Configure which memory tiers are active. Each tier can be independently enabled or disabled.")
              : memoryLockReason}{" "}
            {t_i18n("Enabled tiers are injected into every assignment run and chat message.")}
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          {MEMORY_TIERS.map(({ field, label, description }) => {
            // Read-only rows read the persisted value straight off `agent` so the
            // displayed status always mirrors the backend — the draft state is
            // seeded once on mount and never updated when there is nothing to save.
            const enabled = canManage ? memoryDraft[field] : agent[field];
            return (
              <div key={field} className="flex items-center justify-between gap-4">
                <div className="space-y-0.5">
                  <p className="content-base font-medium">{t_i18n(label)}</p>
                  <p className="content-caption text-default-secondary">{description(formatter)}</p>
                </div>
                {canManage ? (
                  <Switch aria-label={t_i18n(label)}
                    checked={enabled}
                    onCheckedChange={(v) => onSaveMemoryTier(field, v)}
                    disabled={isPending}
                  />
                ) : (
                  <div className="flex items-center gap-2 shrink-0">
                    <span className={cn("content-caption font-medium", enabled ? "text-accent" : "text-default-secondary")}>
                      {enabled ? t_i18n("Enabled") : t_i18n("Disabled")}
                    </span>
                    {/* `wrap` — a disabled Switch renders <button disabled>, which
                        swallows hover/focus and would never fire the tooltip. */}
                    <Tip content={memoryLockReason} wrap>
                      <Switch aria-label={t_i18n(label)} checked={enabled} disabled aria-readonly="true" />
                    </Tip>
                  </div>
                )}
              </div>
            );
          })}
        </CardContent>
      </Card>
    </>
  );
}
