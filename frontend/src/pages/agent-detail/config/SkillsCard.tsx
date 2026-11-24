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

import { useMemo, useState } from "react";
import {
  Building2,
  CheckCircle2,
  ExternalLink,
  GraduationCap,
  Link as LinkIcon,
  Pin,
  Plus,
  RefreshCw,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { Button } from "@/components/ui/button";
import { CardEditActions } from "@/components/ui/card-edit-actions";
import { Input } from "@/components/ui/input";
import { Tip } from "@/components/ui/tooltip";
import { SearchInput } from "@/components/ui/search-input";
import { useToast } from "@/components/ui/toast";
import { useSkills, useCreateSkill, useRefreshSkill, useSkillPackageBindings } from "@/hooks/api/useSkillApi";
import { useFormatter } from "@/i18n";
import { withBoundEntities } from "@/lib/entity-list-scope";
import { packageResourceSummary } from "@/lib/skillPackage";
import { cn, toggleArrayItem } from "@/lib/utils";
import { safeHttpUrl } from "@/lib/safe-url";
import { CONTENT_PREVIEW_LIMIT } from "@/lib/constants";
import type { Agent } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { formatTimeAgo } from "@/lib/relative-time";
import { Spinner } from "@filigran/design-system";

export function SkillsCard({
  agent,
  skillIds,
  setSkillIds,
  skillLoadModes,
  setSkillLoadModes,
  editing,
  canManage,
  onStartEdit,
  onSave,
  onCancel,
  saving,
  companyManaged,
}: {
  agent: Agent;
  skillIds: string[];
  setSkillIds: React.Dispatch<React.SetStateAction<string[]>>;
  skillLoadModes: Record<string, "progressive" | "always">;
  setSkillLoadModes: React.Dispatch<React.SetStateAction<Record<string, "progressive" | "always">>>;
  editing: boolean;
  canManage: boolean;
  onStartEdit: () => void;
  onSave: () => void;
  onCancel: () => void;
  saving: boolean;
  companyManaged: boolean;
}) {
  const { t_i18n, intl } = useFormatter();
  // The viewer's skills plus the ones the agent is bound to (an admin on a
  // colleague's agent sees and keeps them).
  const { data: visibleSkills } = useSkills();
  const { data: everySkill } = useSkills({ scope: "all" });
  const allSkills = useMemo(
    () => withBoundEntities(visibleSkills, everySkill, agent.skill_ids),
    [visibleSkills, everySkill, agent.skill_ids],
  );
  const { data: packageBindings } = useSkillPackageBindings();
  const createSkill = useCreateSkill();
  const aiOff = useAiActionDisabled();
  const refreshSkillMut = useRefreshSkill();
  const { toast } = useToast();

  const [showQuickAdd, setShowQuickAdd] = useState(false);
  const [newUrl, setNewUrl] = useState("");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [skillSearch, setSkillSearch] = useState("");

  // Skills available for this agent (respects company-managed constraint, filtered + sorted)
  const allAvailableSkills = allSkills?.filter((s) => s.enabled && (!companyManaged || s.is_company_managed)) ?? [];
  const availableSkills = allAvailableSkills
    .filter((s) => {
      if (skillSearch) {
        const q = skillSearch.toLowerCase();
        return s.name.toLowerCase().includes(q) || (s.description ?? "").toLowerCase().includes(q);
      }
      return true;
    })
    .sort((a, b) => a.name.localeCompare(b.name));

  const toggleSkill = (id: string) => {
    setSkillIds((prev) => toggleArrayItem(prev, id));
    // Detaching drops the pin: keeping it would silently re-pin the skill if
    // the user attaches it again later.
    setSkillLoadModes((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  };

  // Pinning keeps a skill's body inline in the system prompt on every turn
  // instead of letting the agent pull it in with load_skill when it matches
  // the task. Detaching a skill drops its pin — the backend prunes orphans
  // anyway, but leaving it here would make the pin reappear on re-attach.
  const togglePinned = (id: string) =>
    setSkillLoadModes((prev) => {
      const next = { ...prev };
      if (next[id] === "always") delete next[id];
      else next[id] = "always";
      return next;
    });

  // What each skill hands to the agent along with its instructions: attaching a
  // packaged skill also attaches the Custom Tools / KBs its ``scripts/`` and
  // ``references/`` were imported into, and detaching it takes them away.
  const packageSummary = (skillId: string) =>
    packageResourceSummary(packageBindings?.find((b) => b.skill_id === skillId), t_i18n);
  const anyAttachedCarriesResources = (agent.skill_ids || []).some((id) => packageSummary(id));

  const handleQuickAdd = async () => {
    if (!newUrl.trim()) return;
    try {
      const created = await createSkill.mutateAsync({ url: newUrl.trim() });
      toast({ variant: "success", title: t_i18n("Skill created"), description: t_i18n('"{name}" has been fetched and is ready to attach.', { name: created.name }) });
      setNewUrl("");
      setShowQuickAdd(false);
      // Auto-attach the newly created skill
      setSkillIds((prev) => [...prev, created.id]);
    } catch (err) {
      toast({ variant: "error", title: t_i18n("Failed to create skill"), description: err instanceof Error ? err.message : t_i18n("Unknown error") });
    }
  };

  // Resolve currently attached skills for read-only view (sorted alphabetically)
  const attachedSkills = (agent.skill_ids || [])
    .map((id) => allSkills?.find((s) => s.id === id))
    .filter((s): s is NonNullable<typeof s> => !!s)
    .sort((a, b) => a.name.localeCompare(b.name));

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
        <div>
          <CardTitle className="content-base flex items-center gap-2">
            <GraduationCap className="h-4 w-4 text-accent" /> {t_i18n("Skills")}
            {attachedSkills.length > 0 && !editing && (
              <CountBadge variant="secondary">{attachedSkills.length}</CountBadge>
            )}
          </CardTitle>
          <p className="content-caption text-default-secondary mt-0.5">
            {intl.formatMessage(
              { id: "Shared instruction sets ({link}) injected into the agent's context at runtime." },
              { link: <a href="https://agentskills.io" target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">agentskills.io</a> },
            )}
          </p>
          {companyManaged && <p className="content-caption text-feedback-warning-primary mt-0.5 flex items-center gap-1"><Building2 className="h-3 w-3" /> {t_i18n("Only company-managed skills are shown.")}</p>}
        </div>
        <CardEditActions editing={editing} canEdit={canManage} onEdit={onStartEdit} onCancel={() => { setSkillSearch(""); onCancel(); }} onSave={() => { setSkillSearch(""); onSave(); }} saveDisabled={saving} />
      </CardHeader>
      <CardContent className="px-4 pb-4 pt-0 space-y-3">
        {editing ? (
          <>
            <p className="content-caption text-default-secondary mb-2">
              {t_i18n("Click to attach or detach skills. Or quickly add a new skill by URL.")}{" "}
              {t_i18n("Attached skills are listed in the agent's prompt and loaded on demand; use the pin to keep one always loaded.")}
            </p>
            <p className="content-caption text-default-secondary mb-2">
              {intl.formatMessage(
                { id: "A skill tagged {badge} ships packaged resources: attaching it also attaches those knowledge bases and custom tools, and detaching it removes them again." },
                { badge: <Badge variant="secondary" className="content-caption leading-none px-1 py-0 align-middle">1 KB · 2 tools</Badge> },
              )}
            </p>
            {allAvailableSkills.length > 3 && (
              <SearchInput placeholder={t_i18n("Filter skills...")} value={skillSearch} onChange={setSkillSearch} className="mb-3 max-w-full" />
            )}

            {/* Quick-add URL form */}
            {showQuickAdd ? (
              <div className="rounded-sm border border-accent/20 bg-accent/5 p-3 space-y-2 mb-3">
                <p className="content-compact font-medium text-accent">{t_i18n("Quick Add by URL")}</p>
                <p className="content-caption text-default-secondary">
                  {t_i18n("Paste a URL to a SKILL.md file. Name and description are auto-detected from YAML frontmatter.")}
                </p>
                <div className="flex gap-2">
                  <Input aria-label={t_i18n("Quick Add by URL")}
                    placeholder="https://raw.githubusercontent.com/org/repo/main/skills/my-skill/SKILL.md"
                    value={newUrl}
                    onChange={(e) => setNewUrl(e.target.value)}
                    className="content-compact h-8"
                    onKeyDown={(e) => { if (e.key === "Enter") handleQuickAdd(); }}
                  />
                  <AiDisabledTip>
                  <Button
                    size="sm"
                    className="h-8 content-compact gap-1.5 shrink-0"
                    onClick={handleQuickAdd}
                    disabled={createSkill.isPending || !newUrl.trim() || aiOff} aria-busy={createSkill.isPending || undefined}>
                    {createSkill.isPending && <Spinner size="sm" tone="inherit" />}{!createSkill.isPending && <Plus className="h-3 w-3" />}
                    {t_i18n("Create & Attach")}
                  </Button>
                  </AiDisabledTip>
                  <Button variant="ghost" size="sm" className="h-8 content-compact shrink-0" onClick={() => { setShowQuickAdd(false); setNewUrl(""); }}>
                    {t_i18n("Cancel")}
                  </Button>
                </div>
              </div>
            ) : (
              <Button variant="outline" size="sm" className="h-7 content-compact gap-1.5 mb-2" onClick={() => setShowQuickAdd(true)}>
                <LinkIcon className="h-3 w-3" /> {t_i18n("Add new skill by URL")}
              </Button>
            )}

            {/* Skill picker grid */}
            {availableSkills.length > 0 ? (
              <div className="grid gap-2 sm:grid-cols-3">
                {availableSkills.map((skill) => {
                  const isAttached = skillIds.includes(skill.id);
                  const isPinned = skillLoadModes[skill.id] === "always";
                  const carries = packageSummary(skill.id);
                  return (
                    // A div, not a button: the pin control below is itself a
                    // button and nesting buttons is invalid HTML.
                    <div
                      key={skill.id}
                      role="button"
                      tabIndex={0}
                      aria-pressed={isAttached}
                      aria-label={t_i18n(isAttached ? "Detach skill {name}" : "Attach skill {name}", { name: skill.name })}
                      className={cn(
                        "flex items-center gap-3 rounded-sm border p-3 text-left transition-all cursor-pointer",
                        isAttached ? "border-accent/40 bg-accent/5" : "border-elevation-subtle-soft bg-elevation-hover hover:border-elevation-subtle",
                      )}
                      onClick={() => toggleSkill(skill.id)}
                      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleSkill(skill.id); } }}
                    >
                      <GraduationCap className={cn("h-5 w-5 shrink-0", isAttached ? "text-accent" : "text-default-secondary")} />
                      <div className="min-w-0 flex-1">
                        <p className={cn("content-base font-medium truncate", isAttached && "text-accent")}>{skill.name}</p>
                        {skill.description && <p className="content-caption text-default-secondary truncate">{skill.description}</p>}
                        {carries && (
                          <Badge variant="secondary" className="content-caption leading-none px-1 mt-0.5">{carries}</Badge>
                        )}
                        {skill.compatibility && <p className="content-caption text-feedback-warning-primary truncate mt-0.5">⚠ {skill.compatibility}</p>}
                      </div>
                      {isAttached && (
                        <Tip content={t_i18n(isPinned
                          ? "Always loaded: the full instructions sit in the system prompt on every turn."
                          : "On demand: the agent loads the full instructions when the task matches. Click to always load instead.")}
                        >
                          <button
                            type="button"
                            aria-label={t_i18n(isPinned ? "Switch to on-demand loading" : "Always load this skill")}
                            aria-pressed={isPinned}
                            className={cn(
                              "p-1 rounded-sm transition-colors shrink-0",
                              isPinned ? "text-accent" : "text-default-disabled hover:text-default-secondary",
                            )}
                            onClick={(e) => { e.stopPropagation(); togglePinned(skill.id); }}
                          >
                            <Pin className={cn("h-3.5 w-3.5", isPinned && "fill-current")} />
                          </button>
                        </Tip>
                      )}
                      {isAttached ? (
                        <CheckCircle2 className="h-4 w-4 text-accent shrink-0" />
                      ) : (
                        <Plus className="h-4 w-4 text-default-disabled shrink-0" />
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="py-4 text-center">
                <GraduationCap className="h-6 w-6 text-default-disabled mx-auto mb-1.5" />
                <p className="content-base text-default-secondary">{t_i18n("No skills available")}</p>
                <p className="content-caption text-default-secondary mt-0.5">{t_i18n("Create one using \"Add new skill by URL\" above, or in the Skills page.")}</p>
              </div>
            )}
          </>
        ) : (
          <div>
            {attachedSkills.length > 0 ? (
              <div className="space-y-1.5">
                {attachedSkills.map((skill) => {
                  if (!skill) return null;
                  const isExpanded = expandedId === skill.id;
                  return (
                    <div key={skill.id} className="rounded-sm bg-elevation-hover overflow-hidden">
                      <div
                        className="flex items-center gap-3 px-3 py-2.5 cursor-pointer hover:bg-elevation-hover transition-colors"
                        onClick={() => setExpandedId(isExpanded ? null : skill.id)}
                      >
                        <GraduationCap className="h-4 w-4 text-accent shrink-0" />
                        <div className="min-w-0 flex-1">
                          <p className="content-base font-medium truncate">{skill.name}</p>
                          {skill.description && <p className="content-caption text-default-secondary truncate">{skill.description}</p>}
                        </div>
                        <div className="flex items-center gap-1.5 shrink-0">
                          {packageSummary(skill.id) && (
                            <Tip content={t_i18n("Packaged resources this skill also attaches to the agent (knowledge bases and custom tools). They are removed when the skill is detached.")}>
                              <Badge variant="secondary" className="content-caption leading-none px-1">{packageSummary(skill.id)}</Badge>
                            </Tip>
                          )}
                          {agent.skill_load_modes?.[skill.id] === "always" && (
                            <Tip content={t_i18n("Always loaded: the full instructions sit in the system prompt on every turn.")}>
                              <Pin className="h-3 w-3 text-accent fill-current" />
                            </Tip>
                          )}
                          {skill.compatibility && (
                            <Tip content={skill.compatibility}><span className="content-caption text-feedback-warning-primary">⚠</span></Tip>
                          )}
                          {skill.fetched_at && (
                            <span className="content-caption text-default-secondary">{formatTimeAgo(intl, skill.fetched_at)}</span>
                          )}
                          {skill.is_company_managed && <Building2 className="h-3 w-3 text-default-disabled" />}
                          {skill.url && (
                            <Tip content={t_i18n("Refresh content from URL")}>
                              <button aria-label={t_i18n("Refresh content from URL")}
                                className="p-1 text-default-secondary hover:text-accent transition-colors"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  refreshSkillMut.mutate(skill.id, {
                                    onSuccess: () => toast({ variant: "success", title: t_i18n("Skill refreshed") }),
                                  });
                                }}
                              >
                                <RefreshCw className="h-3 w-3" />
                              </button>
                            </Tip>
                          )}
                        </div>
                      </div>
                      {isExpanded && (
                        <div className="border-t border-elevation-subtle-soft px-3 py-2.5 space-y-2">
                          {skill.url ? (
                            <SkillSourceLink url={skill.url} />
                          ) : (
                            <p className="content-caption text-default-secondary italic">{t_i18n("Direct content (no URL source)")}</p>
                          )}
                          {skill.compatibility && (
                            <p className="content-caption text-feedback-warning-primary">{t_i18n("Compatibility: {value}", { value: skill.compatibility })}</p>
                          )}
                          {skill.content && (
                            <div className="rounded-sm bg-elevation-hover p-2 max-h-48 overflow-auto">
                              <pre className="content-caption text-default-secondary whitespace-pre-wrap font-mono-plex leading-relaxed">{skill.content.length > CONTENT_PREVIEW_LIMIT ? skill.content.slice(0, CONTENT_PREVIEW_LIMIT) + "\n\n" + t_i18n("... (truncated)") : skill.content}</pre>
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="content-base text-default-secondary italic py-3 text-center">{t_i18n("No skills attached")}</p>
            )}
            {anyAttachedCarriesResources && (
              <p className="content-caption text-default-secondary mt-2">
                {t_i18n("Badged skills also attach their packaged knowledge bases and custom tools to this agent: they appear in the sections below and are removed when the skill is detached.")}
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** A skill's source URL: a link only when it is a web address, else the text (`safeHttpUrl`). */
function SkillSourceLink({ url }: { url: string }) {
  const href = safeHttpUrl(url);
  return (
    <div className="flex items-center gap-1.5 content-caption text-default-secondary">
      <LinkIcon className="h-3 w-3" />
      {href ? (
        <>
          <a href={href} target="_blank" rel="noopener noreferrer" className="hover:text-accent truncate">{url}</a>
          <ExternalLink className="h-2.5 w-2.5 shrink-0" />
        </>
      ) : (
        <span className="truncate">{url}</span>
      )}
    </div>
  );
}
