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
import { Slider } from "@filigran/design-system";
import {
  Cog, Bot, Tag, BookOpen, Database, HandMetal, Bell, BarChart3,
  Hourglass, RefreshCw, Check, Lock, Repeat, FlaskConical,
  ChevronDown, ChevronUp, RotateCcw, MonitorCog, Monitor, Circle, Building2,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { useUpdateAssignment } from "@/hooks/api/useAssignmentApi";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { useGeneralAssistantConfig } from "@/hooks/api/useSettingsApi";
import { useRunners } from "@/hooks/api/useRunnerApi";
import { useRunnersEnabled } from "@/hooks/useDesktop";
import { useToast } from "@/components/ui/toast";
import { withBoundEntities } from "@/lib/entity-list-scope";
import { cn } from "@/lib/utils";
import {
  DEFAULT_AI_LABEL_PROMPT, DEFAULT_HITL_PROMPT,
  MAX_ITERATIONS_CEILING, MAX_ITERATIONS_DEFAULT, ORGANIZATION_CONTEXT_MODE_LABELS, clampMaxIterations,
} from "@/lib/constants";
import type { Assignment, Agent, KnowledgeBase, OrganizationContextMode } from "@/types";
import { OrganizationContextModeSelect, useOrganizationContextEffectiveLabel } from "@/components/organization-context-mode-select";
import { useFormatter } from "@/i18n";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { PromptGenerator } from "@/components/ui/prompt-generator";
import { ConfigCard } from "./ConfigCard";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { CardEditActions } from "@/components/ui/card-edit-actions";

// ---------------------------------------------------------------------------
// ParametersCard — Assignment execution parameters (edit + read-only)
// ---------------------------------------------------------------------------

interface ParametersCardProps {
  assignment: Assignment;
  agent: Agent | undefined;
  agentKbs: KnowledgeBase[];
  canManage: boolean;
  onUpdate: () => void;
}

export function ParametersCard({
  assignment, agent, agentKbs, canManage, onUpdate,
}: ParametersCardProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateAssignment();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  const aiOff = useAiActionDisabled();
  const { data: gaConfig } = useGeneralAssistantConfig();

  // ── Runner refinement (native OS-control) ──
  // Only meaningful when the deployment has Runners enabled AND the agent binds
  // the ``computer_use`` tool. The assignment can only refine WITHIN the set the
  // agent may target (mirrors the KB write-refinement contract). Not while
  // `/auth/config` loads: `/runners` answers 404 on a deployment without them.
  const runnersEnabled = useRunnersEnabled() === true;
  const agentHasComputerUse = (agent?.builtin_tool_ids || []).includes("computer_use");
  const showRunners = runnersEnabled && agentHasComputerUse;
  // The viewer's machines plus the ones the agent and this assignment already
  // target (an admin on a colleague's assignment sees and keeps them).
  // Undefined until every bound machine is known: the edit-time clamp below
  // reads it as "loaded" and would otherwise drop a bound machine.
  const { data: visibleRunners } = useRunners({ enabled: showRunners });
  const { data: everyRunner } = useRunners({ enabled: showRunners, scope: "all" });
  const allRunners = useMemo(
    () => withBoundEntities(visibleRunners, everyRunner, [
      ...(agent?.runner_ids ?? []),
      ...(assignment.runner_ids ?? []),
    ]),
    [visibleRunners, everyRunner, agent?.runner_ids, assignment.runner_ids],
  );
  // The agent's targetable set: its explicit runner_ids (if any), else every
  // visible runner, then narrowed by any agent runner_labels (AND).
  const agentRunners = (allRunners ?? []).filter((r) => {
    const ids = agent?.runner_ids ?? [];
    if (ids.length > 0 && !ids.includes(r.id)) return false;
    const labels = agent?.runner_labels ?? [];
    if (labels.length > 0 && !(r.labels ?? []).some((l) => labels.includes(l))) return false;
    return true;
  });
  const agentRunnerIdSet = new Set(agentRunners.map((r) => r.id));
  // Clamp into [1, 100] so a legacy/out-of-band value (persisted before the
  // server-side clamp existed) never feeds the range inputs / inherit readouts
  // an out-of-range value; the server also clamps on the next save.
  const platformMaxIterations = clampMaxIterations(
    gaConfig?.max_iterations ?? MAX_ITERATIONS_DEFAULT,
  );
  const agentMaxIterations =
    agent?.max_iterations != null ? clampMaxIterations(agent.max_iterations) : null;
  // What the assignment inherits when its own max_iterations is null:
  // the agent's override, else the platform default.
  const inheritedMaxIterations = agentMaxIterations ?? platformMaxIterations;

  const [editing, setEditing] = useState(false);

  // Agent KBs the agent is allowed to write to (permission set on the agent).
  // The assignment can only refine within this set.
  const writableKbs = agentKbs.filter(
    (kb) => agent?.knowledge_base_permissions?.[kb.id] === "write",
  );
  // Execution intersects an assignment's write_knowledge_base_ids with the
  // agent's currently-writable set, so the read-only card must reflect that
  // intersection (not the raw stored ids) or it can show "On" / a KB name for
  // a selection whose writes are actually dropped at runtime.
  const writableIdSet = new Set(writableKbs.map((kb) => kb.id));

  // ── Editable fields ──
  const [aiLabel, setAiLabel] = useState(false);
  const [aiLabelPrompt, setAiLabelPrompt] = useState<string | null>(null);
  const [showAiLabelPrompt, setShowAiLabelPrompt] = useState(false);
  // Read: inherit ALL agent KBs (null) vs refine to a subset.
  const [readInheritAll, setReadInheritAll] = useState(true);
  const [readKbIds, setReadKbIds] = useState<string[]>([]);
  // Write: inherit ALL agent-writable KBs (null) vs refine to a subset.
  const [writeInheritAll, setWriteInheritAll] = useState(true);
  const [writeKbIds, setWriteKbIds] = useState<string[]>([]);
  // Runners: inherit the agent's runner selection (null) vs refine to a subset
  // ([] = none). Same tri-state contract as the write-KB refinement.
  const [runnerInheritAll, setRunnerInheritAll] = useState(true);
  const [runnerIds, setRunnerIds] = useState<string[]>([]);
  const [notify, setNotify] = useState(false);
  const [showDigest, setShowDigest] = useState(false);
  const [humanInTheLoop, setHumanInTheLoop] = useState<boolean | null>(null);
  const [hitlPrompt, setHitlPrompt] = useState<string | null>(null);
  const [showHitlPrompt, setShowHitlPrompt] = useState(false);
  const [timeoutSeconds, setTimeoutSeconds] = useState(300);
  const [retryOnFailure, setRetryOnFailure] = useState(false);
  const [maxRetries, setMaxRetries] = useState(3);
  const [dryRun, setDryRun] = useState(false);
  // null = inherit (agent override -> platform default); a number overrides the agent.
  const [maxIterations, setMaxIterations] = useState<number | null>(null);
  // Organization summary in the run's prompt (#11533): inherit the agent, always or off.
  const [organizationContextMode, setOrganizationContextMode] = useState<OrganizationContextMode>("inherit");
  const organizationContextInEffect = useOrganizationContextEffectiveLabel(
    "assignment",
    assignment.organization_context_mode ?? "inherit",
    agent?.organization_context_mode ?? "inherit",
  );

  const startEditing = () => {
    setAiLabel(assignment.ai_label ?? false);
    setAiLabelPrompt(assignment.ai_label_prompt ?? null);
    setShowAiLabelPrompt(false);
    // Read has no "none" state: null OR an empty list both mean "all agent KBs".
    setReadInheritAll(!assignment.read_knowledge_base_ids || assignment.read_knowledge_base_ids.length === 0);
    setReadKbIds(assignment.read_knowledge_base_ids ?? []);
    setWriteInheritAll(assignment.write_knowledge_base_ids == null);
    // Drop stale/non-writable ids on entering edit mode: if the agent's write
    // grants changed since this assignment was configured, those ids can't be
    // displayed/selected and runtime drops them anyway, so keep the saved
    // selection clean by intersecting with the current writable set.
    setWriteKbIds(
      (assignment.write_knowledge_base_ids ?? []).filter((kid) => writableIdSet.has(kid)),
    );
    // Runner refinement: null = inherit the agent's runners; [ids]/[] = refine.
    // Clamp stale ids the agent can no longer target ONLY once the runners list
    // has loaded. While useRunners() is still pending, agentRunnerIdSet is
    // empty, so clamping here would drop every saved id and a subsequent Save
    // would persist runner_ids: [] (= no runners), silently disabling computer
    // use for the assignment. Preserve the stored ids until the list arrives.
    setRunnerInheritAll(assignment.runner_ids == null);
    setRunnerIds(
      allRunners
        ? (assignment.runner_ids ?? []).filter((rid) => agentRunnerIdSet.has(rid))
        : (assignment.runner_ids ?? []),
    );
    setNotify(assignment.notify_on_complete ?? false);
    setShowDigest(assignment.show_in_digest ?? false);
    setHumanInTheLoop(assignment.human_in_the_loop);
    setHitlPrompt(assignment.human_in_the_loop_prompt ?? null);
    setShowHitlPrompt(false);
    setTimeoutSeconds(assignment.timeout_seconds ?? 300);
    setRetryOnFailure(assignment.retry_on_failure ?? false);
    setMaxRetries(assignment.max_retries ?? 3);
    setDryRun(assignment.dry_run ?? false);
    setMaxIterations(assignment.max_iterations != null ? clampMaxIterations(assignment.max_iterations) : null);
    setOrganizationContextMode(assignment.organization_context_mode ?? "inherit");
    setEditing(true);
  };

  const handleSave = async () => {
    // A variable referenced in these prompts resolves in the assignment's runs.
    if (!(await confirmSharing({
      entity_type: "assignment",
      entity_id: assignment.id,
      change: { ai_label_prompt: aiLabelPrompt, human_in_the_loop_prompt: hitlPrompt },
    }))) {
      return;
    }
    update.mutate(
      {
        id: assignment.id,
        ai_label: aiLabel,
        ai_label_prompt: aiLabelPrompt,
        // Read has no "none": inherit-all OR an empty selection => null (= all agent KBs).
        read_knowledge_base_ids: readInheritAll || readKbIds.length === 0 ? null : readKbIds,
        // null = inherit all agent-writable KBs; [] = none; [ids] = refine.
        write_knowledge_base_ids: writeInheritAll ? null : writeKbIds,
        // null = inherit the agent's runners; [] = none; [ids] = refine.
        ...(showRunners
          ? { runner_ids: runnerInheritAll ? null : runnerIds }
          : {}),
        notify_on_complete: notify,
        show_in_digest: showDigest,
        human_in_the_loop: humanInTheLoop,
        human_in_the_loop_prompt: hitlPrompt,
        timeout_seconds: timeoutSeconds,
        retry_on_failure: retryOnFailure,
        max_retries: maxRetries,
        dry_run: dryRun,
        // null = inherit (agent override -> platform default); a value overrides the agent.
        max_iterations: maxIterations,
        organization_context_mode: organizationContextMode,
      },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Parameters updated"), variant: "success" });
          setEditing(false);
          onUpdate();
        },
      },
    );
  };

  return (
    <Card>
      {consentElement}
      <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
        <CardTitle className="content-base flex items-center gap-2"><Cog className="h-4 w-4 text-accent" /> {t_i18n("Parameters")}</CardTitle>
        <CardEditActions
          editing={editing}
          canEdit={canManage}
          aiGated
          onEdit={startEditing}
          onCancel={() => setEditing(false)}
          onSave={handleSave}
          saveDisabled={update.isPending || aiOff}
        />
      </CardHeader>
      <CardContent className="px-4 pb-4 pt-0">
        {editing ? (
          <div className="space-y-5">
            {/* ── A: AI-generated label ── */}
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <Tag className="h-3.5 w-3.5 text-feedback-info-primary" />
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("AI-generated label")}</p>
              </div>
              <div className="flex items-center justify-between py-2.5">
                <div><p className="content-compact font-medium">{t_i18n("Prepend AI label to messages")}</p><p className="content-caption text-default-secondary mt-0.5">{t_i18n("Add a visible tag to outgoing messages indicating AI authorship.")}</p></div>
                <Switch aria-label={t_i18n("Prepend AI label to messages")} checked={aiLabel} onCheckedChange={setAiLabel} />
              </div>
              {aiLabel && (
                <div className="space-y-2">
                  <button type="button" className="flex items-center gap-1.5 content-caption text-default-secondary hover:text-default-primary transition-colors" onClick={() => setShowAiLabelPrompt((v) => !v)}>
                    {showAiLabelPrompt ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                    {t_i18n("System prompt addition")} {aiLabelPrompt ? t_i18n("(customized)") : t_i18n("(default)")}
                  </button>
                  {showAiLabelPrompt && (
                    <div className="space-y-1.5">
                      <div className="flex justify-end">
                        <PromptGenerator
                          contextType="ai_label_prompt"
                          currentText={aiLabelPrompt ?? DEFAULT_AI_LABEL_PROMPT}
                          onGenerated={setAiLabelPrompt}
                          agentName={agent?.name}
                          assignmentName={assignment.name}
                        />
                      </div>
                      <textarea
                        className={`w-full min-h-[80px] rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 content-compact text-default-primary placeholder:text-default-secondary ${FIELD_FOCUS_RING} resize-y`}
                        value={aiLabelPrompt ?? DEFAULT_AI_LABEL_PROMPT}
                        onChange={(e) => setAiLabelPrompt(e.target.value)}
                      />
                      {aiLabelPrompt && (
                        <button type="button" className="flex items-center gap-1 content-caption text-accent hover:text-accent/80" onClick={() => setAiLabelPrompt(null)}>
                          <RotateCcw className="h-3 w-3" /> {t_i18n("Reset to default")}
                        </button>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>

            <div className="border-t border-elevation-subtle-soft" />

            {/* ── C: Read knowledge bases ── */}
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <BookOpen className="h-3.5 w-3.5 text-feedback-success-primary" />
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Read knowledge bases (RAG)")}</p>
              </div>
              {agentKbs.length === 0 ? (
                <p className="content-caption text-default-secondary italic">{t_i18n("No knowledge bases are bound to this agent. Bind some in the agent's Knowledge configuration first.")}</p>
              ) : (
                <>
                  <div className="flex items-center justify-between py-2.5">
                    <div><p className="content-compact font-medium">{t_i18n("Use all of the agent's knowledge bases")}</p><p className="content-caption text-default-secondary mt-0.5">{t_i18n("Search every knowledge base bound to the agent. Turn off to restrict this assignment to a subset.")}</p></div>
                    <Switch aria-label={t_i18n("Use all of the agent's knowledge bases")} checked={readInheritAll} onCheckedChange={setReadInheritAll} />
                  </div>
                  {!readInheritAll && (
                    <div className="space-y-1.5">
                      {agentKbs.map((kb) => {
                        const isSelected = readKbIds.includes(kb.id);
                        return (
                          <button
                            key={kb.id}
                            type="button"
                            className={cn(
                              "w-full flex items-center gap-2.5 rounded-sm px-3 py-2 content-compact text-left transition-colors border",
                              isSelected
                                ? "border-accent/30 bg-accent/5 text-default-primary"
                                : "border-elevation-subtle-soft bg-elevation-hover text-default-secondary hover:border-elevation-subtle hover:bg-elevation-highlight",
                            )}
                            onClick={() => {
                              setReadKbIds((prev) =>
                                isSelected ? prev.filter((id) => id !== kb.id) : [...prev, kb.id],
                              );
                            }}
                          >
                            <div className={cn("h-3.5 w-3.5 rounded-sm border flex items-center justify-center shrink-0", isSelected ? "border-accent bg-accent" : "border-elevation-subtle-soft")}>
                              {isSelected && <Check className="h-2.5 w-2.5 text-negative-primary" />}
                            </div>
                            <span className="truncate">{kb.name}</span>
                          </button>
                        );
                      })}
                      {readKbIds.length === 0 && (
                        <p className="content-caption text-default-secondary italic">{t_i18n("No knowledge bases selected: this assignment will search all of the agent's knowledge bases.")}</p>
                      )}
                    </div>
                  )}
                </>
              )}
            </div>

            <div className="border-t border-elevation-subtle-soft" />

            {/* ── D: Write knowledge bases ── */}
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <Database className="h-3.5 w-3.5 text-accent" />
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Write knowledge bases")}</p>
              </div>
              {writableKbs.length === 0 ? (
                <div className="flex items-start gap-2 rounded-sm bg-elevation-hover border border-elevation-subtle-soft px-3 py-2.5">
                  <Lock className="h-3.5 w-3.5 text-default-secondary mt-0.5 shrink-0" />
                  <p className="content-caption text-default-secondary leading-relaxed">
                    {t_i18n("This agent has no writable knowledge bases, so this assignment cannot write. To allow writing, enable")} <span className="text-default-primary">{t_i18n("write access")}</span> {t_i18n("on a knowledge base in the agent's")} <span className="text-default-primary">{t_i18n("Knowledge")}</span> {t_i18n("configuration, then refine it here.")}
                  </p>
                </div>
              ) : (
                <>
                  <div className="flex items-center justify-between py-2.5">
                    <div><p className="content-compact font-medium">{t_i18n("Use all of the agent's writable knowledge bases")}</p><p className="content-caption text-default-secondary mt-0.5">{t_i18n("Let this assignment write to every KB the agent can write to (via")} <span className="font-medium">store_knowledge</span>{t_i18n("). Turn off to restrict to a subset, or select none to disable writing.")}</p></div>
                    <Switch aria-label={t_i18n("Use all of the agent's writable knowledge bases")} checked={writeInheritAll} onCheckedChange={setWriteInheritAll} />
                  </div>
                  {!writeInheritAll && (
                    <div className="space-y-1.5">
                      {writableKbs.map((kb) => {
                        const isSelected = writeKbIds.includes(kb.id);
                        return (
                          <button
                            key={kb.id}
                            type="button"
                            className={cn(
                              "w-full flex items-center gap-2.5 rounded-sm px-3 py-2 content-compact text-left transition-colors border",
                              isSelected
                                ? "border-accent/30 bg-accent/5 text-default-primary"
                                : "border-elevation-subtle-soft bg-elevation-hover text-default-secondary hover:border-elevation-subtle hover:bg-elevation-highlight",
                            )}
                            onClick={() => {
                              setWriteKbIds((prev) =>
                                isSelected ? prev.filter((id) => id !== kb.id) : [...prev, kb.id],
                              );
                            }}
                          >
                            <div className={cn("h-3.5 w-3.5 rounded-sm border flex items-center justify-center shrink-0", isSelected ? "border-accent bg-accent" : "border-elevation-subtle-soft")}>
                              {isSelected && <Check className="h-2.5 w-2.5 text-negative-primary" />}
                            </div>
                            <span className="truncate">{kb.name}</span>
                          </button>
                        );
                      })}
                      {writeKbIds.length === 0 && (
                        <p className="content-caption text-default-secondary italic">{t_i18n("No knowledge bases selected: this assignment will not write to any knowledge base.")}</p>
                      )}
                    </div>
                  )}
                </>
              )}
            </div>

            {showRunners && (
              <>
                <div className="border-t border-elevation-subtle-soft" />

                {/* ── D2: Runners (native OS-control) ── */}
                <div className="space-y-3">
                  <div className="flex items-center gap-2">
                    <MonitorCog className="h-3.5 w-3.5 text-accent" />
                    <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Runners (computer use)")}</p>
                  </div>
                  {agentRunners.length === 0 ? (
                    <div className="flex items-start gap-2 rounded-sm bg-elevation-hover border border-elevation-subtle-soft px-3 py-2.5">
                      <Lock className="h-3.5 w-3.5 text-default-secondary mt-0.5 shrink-0" />
                      <p className="content-caption text-default-secondary leading-relaxed">
                        {t_i18n("The agent cannot target any runner right now (none enrolled, or its runner selector matches nothing). Enroll a runner or widen the agent's selection first, then refine it here.")}
                      </p>
                    </div>
                  ) : (
                    <>
                      <div className="flex items-center justify-between py-2.5">
                        <div><p className="content-compact font-medium">{t_i18n("Use all of the agent's runners")}</p><p className="content-caption text-default-secondary mt-0.5">{t_i18n("Let this assignment drive every machine the agent may target. Turn off to restrict to a subset, or select none to disable computer use for this assignment.")}</p></div>
                        <Switch aria-label={t_i18n("Use all of the agent's runners")} checked={runnerInheritAll} onCheckedChange={setRunnerInheritAll} />
                      </div>
                      {!runnerInheritAll && (
                        <div className="space-y-1.5">
                          {agentRunners.map((r) => {
                            const isSelected = runnerIds.includes(r.id);
                            return (
                              <button
                                key={r.id}
                                type="button"
                                className={cn(
                                  "w-full flex items-center gap-2.5 rounded-sm px-3 py-2 content-compact text-left transition-colors border",
                                  isSelected
                                    ? "border-accent/30 bg-accent/5 text-default-primary"
                                    : "border-elevation-subtle-soft bg-elevation-hover text-default-secondary hover:border-elevation-subtle hover:bg-elevation-highlight",
                                )}
                                onClick={() => {
                                  setRunnerIds((prev) =>
                                    isSelected ? prev.filter((id) => id !== r.id) : [...prev, r.id],
                                  );
                                }}
                              >
                                <div className={cn("h-3.5 w-3.5 rounded-sm border flex items-center justify-center shrink-0", isSelected ? "border-accent bg-accent" : "border-elevation-subtle-soft")}>
                                  {isSelected && <Check className="h-2.5 w-2.5 text-negative-primary" />}
                                </div>
                                <Monitor className="h-3.5 w-3.5 shrink-0 text-default-secondary" />
                                <span className="truncate">{r.name}</span>
                                <Circle className={cn("h-2 w-2 ml-auto shrink-0", r.online ? "fill-feedback-success-primary text-feedback-success-primary" : "fill-disabled text-default-disabled")} />
                              </button>
                            );
                          })}
                          {runnerIds.length === 0 && (
                            <p className="content-caption text-default-secondary italic">{t_i18n("No runners selected: this assignment will not be able to use computer-use tools.")}</p>
                          )}
                        </div>
                      )}
                    </>
                  )}
                </div>
              </>
            )}

            <div className="border-t border-elevation-subtle-soft" />

            {/* ── E: Human in the loop ── */}
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <HandMetal className="h-3.5 w-3.5 text-feedback-warning-primary" />
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Human in the loop")}</p>
              </div>
              <div className="space-y-2">
                <Select
                  value={humanInTheLoop === null ? "inherit" : humanInTheLoop ? "on" : "off"}
                  onValueChange={(v) => {
                    if (v === "inherit") { setHumanInTheLoop(null); setHitlPrompt(null); }
                    else if (v === "on") setHumanInTheLoop(true);
                    else setHumanInTheLoop(false);
                  }}
                >
                  <SelectTrigger aria-label={t_i18n("Human in the loop")}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="inherit">{t_i18n("Inherit from agent ({state})", { state: agent?.human_in_the_loop ? t_i18n("On") : t_i18n("Off") })}</SelectItem>
                    <SelectItem value="on">{t_i18n("On (override)")}</SelectItem>
                    <SelectItem value="off">{t_i18n("Off (override)")}</SelectItem>
                  </SelectContent>
                </Select>
                <p className="content-caption text-default-secondary">{t_i18n("Allow the AI to pause and ask for your input when uncertain. Inherits from agent unless overridden.")}</p>
              </div>
              {(humanInTheLoop === true || (humanInTheLoop === null && agent?.human_in_the_loop)) && (
                <div className="space-y-2">
                  <button type="button" className="flex items-center gap-1.5 content-caption text-default-secondary hover:text-default-primary transition-colors" onClick={() => setShowHitlPrompt((v) => !v)}>
                    {showHitlPrompt ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                    {t_i18n("System prompt addition")} {hitlPrompt ? t_i18n("(customized)") : t_i18n("(default)")}
                  </button>
                  {showHitlPrompt && (
                    <div className="space-y-1.5">
                      <div className="flex justify-end">
                        <PromptGenerator
                          contextType="hitl_prompt"
                          currentText={hitlPrompt ?? DEFAULT_HITL_PROMPT}
                          onGenerated={setHitlPrompt}
                          agentName={agent?.name}
                          assignmentName={assignment.name}
                        />
                      </div>
                      <textarea
                        className={`w-full min-h-[140px] rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 content-compact text-default-primary placeholder:text-default-secondary ${FIELD_FOCUS_RING} resize-y`}
                        value={hitlPrompt ?? DEFAULT_HITL_PROMPT}
                        onChange={(e) => setHitlPrompt(e.target.value)}
                      />
                      {hitlPrompt && (
                        <button type="button" className="flex items-center gap-1 content-caption text-accent hover:text-accent/80" onClick={() => setHitlPrompt(null)}>
                          <RotateCcw className="h-3 w-3" /> {t_i18n("Reset to default")}
                        </button>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>

            <div className="border-t border-elevation-subtle-soft" />

            {/* ── F: Output visibility ── */}
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <Bell className="h-3.5 w-3.5 text-feedback-warning-primary" />
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Output visibility")}</p>
              </div>
              <div className="space-y-0">
                <div className="flex items-center justify-between py-2.5 border-b border-elevation-subtle-soft">
                  <div><p className="content-compact font-medium">{t_i18n("Show as notification")}</p><p className="content-caption text-default-secondary mt-0.5">{t_i18n("Bell icon alert when assignment completes.")}</p></div>
                  <Switch aria-label={t_i18n("Show as notification")} checked={notify} onCheckedChange={setNotify} />
                </div>
                <div className="flex items-center justify-between py-2.5">
                  <div><p className="content-compact font-medium">{t_i18n("Show in latest results")}</p><p className="content-caption text-default-secondary mt-0.5">{t_i18n("Appears in the dashboard digest section.")}</p></div>
                  <Switch aria-label={t_i18n("Show in latest results")} checked={showDigest} onCheckedChange={setShowDigest} />
                </div>
              </div>
            </div>

            <div className="border-t border-elevation-subtle-soft" />

            {/* ── G: Max iterations ── */}
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <Repeat className="h-3.5 w-3.5 text-filigran-ia-primary" />
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Max iterations")}</p>
              </div>
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <p className="content-compact font-medium">{t_i18n("Per-run tool-calling rounds")}</p>
                  {maxIterations !== null && (
                    <button type="button" className="flex items-center gap-1 content-caption text-accent hover:text-accent/80" onClick={() => setMaxIterations(null)}>
                      <RotateCcw className="h-3 w-3" /> {t_i18n("Inherit")}
                    </button>
                  )}
                </div>
                <div className="flex items-center gap-3">
                  <Slider
                    min={1} max={MAX_ITERATIONS_CEILING} step={1}
                    value={[maxIterations ?? inheritedMaxIterations]}
                    onValueChange={([value]) => setMaxIterations(value)}
                    aria-label={t_i18n("Per-run tool-calling rounds")}
                    wrapperClassName="flex-1"
                  />
                  <span className="content-base font-mono-plex w-28 text-right tabular-nums">
                    {maxIterations !== null ? maxIterations : t_i18n("Inherit ({value})", { value: inheritedMaxIterations })}
                  </span>
                </div>
                <p className="content-caption text-default-secondary">
                  {t_i18n("Maximum LLM/tool rounds for a single run. Set a value to override the agent for this assignment; leave inherited to follow the {source}.", { source: agentMaxIterations != null ? t_i18n("agent ({value})", { value: agentMaxIterations }) : t_i18n("platform default ({value})", { value: platformMaxIterations }) })}
                </p>
              </div>
            </div>

            <div className="border-t border-elevation-subtle-soft" />

            {/* ── Organization context (#11533) ── */}
            <OrganizationContextModeSelect
              scope="assignment"
              value={organizationContextMode}
              agentMode={agent?.organization_context_mode ?? "inherit"}
              onChange={setOrganizationContextMode}
              fullWidth={false}
            />

            <div className="border-t border-elevation-subtle-soft" />

            {/* ── H: Timeout ── */}
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <Hourglass className="h-3.5 w-3.5 text-feedback-error-primary" />
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Execution timeout")}</p>
              </div>
              <div className="space-y-2">
                <div className="flex items-center gap-3">
                  <Input aria-label={t_i18n("Execution timeout")}
                    type="number"
                    min={0}
                    step={30}
                    className="w-28"
                    value={timeoutSeconds}
                    onChange={(e) => setTimeoutSeconds(parseInt(e.target.value) || 0)}
                  />
                  <span className="content-compact text-default-secondary">{t_i18n("seconds")}</span>
                  <div className="flex gap-1.5">
                    {[60, 300, 600, 0].map((v) => (
                      <button
                        key={v}
                        className={cn("rounded-sm border px-2 py-1 content-caption transition-colors", timeoutSeconds === v ? "border-accent/40 bg-accent/10 text-accent" : "border-elevation-subtle-soft text-default-secondary hover:border-elevation-subtle")}
                        onClick={() => setTimeoutSeconds(v)}
                      >
                        {v === 0 ? t_i18n("No limit") : v === 60 ? t_i18n("1 min") : v === 300 ? t_i18n("5 min") : t_i18n("10 min")}
                      </button>
                    ))}
                  </div>
                </div>
                <p className="content-caption text-default-secondary">{t_i18n("Maximum wall-clock time for a single run. Set to 0 for no limit. Prevents runaway executions.")}</p>
              </div>
            </div>

            <div className="border-t border-elevation-subtle-soft" />

            {/* ── I: Retry on failure ── */}
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <RefreshCw className="h-3.5 w-3.5 text-accent" />
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Retry on failure")}</p>
              </div>
              <div className="flex items-center justify-between py-2.5">
                <div><p className="content-compact font-medium">{t_i18n("Auto-retry failed runs")}</p><p className="content-caption text-default-secondary mt-0.5">{t_i18n("Automatically re-execute the assignment when a run fails (e.g. API timeout, transient error).")}</p></div>
                <Switch aria-label={t_i18n("Auto-retry failed runs")} checked={retryOnFailure} onCheckedChange={setRetryOnFailure} />
              </div>
              {retryOnFailure && (
                <div className="flex items-center gap-3">
                  <label className="content-compact text-default-secondary">{t_i18n("Max retries")}</label>
                  <div className="flex gap-1.5">
                    {[1, 2, 3, 5].map((v) => (
                      <button
                        key={v}
                        className={cn("rounded-sm border px-2.5 py-1 content-caption transition-colors", maxRetries === v ? "border-accent/40 bg-accent/10 text-accent" : "border-elevation-subtle-soft text-default-secondary hover:border-elevation-subtle")}
                        onClick={() => setMaxRetries(v)}
                      >
                        {v}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>

            <div className="border-t border-elevation-subtle-soft" />

            {/* ── J: Dry run ── */}
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <FlaskConical className="h-3.5 w-3.5 text-feedback-warning-primary" />
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Dry run")}</p>
              </div>
              <div className="flex items-center justify-between py-2.5">
                <div><p className="content-compact font-medium">{t_i18n("Simulate without executing actions")}</p><p className="content-caption text-default-secondary mt-0.5">{t_i18n("Every tool call is intercepted and stubbed instead of executed: the agent only describes its plan, and results may contain assumed data.")}</p></div>
                <Switch aria-label={t_i18n("Simulate without executing actions")} checked={dryRun} onCheckedChange={setDryRun} />
              </div>
            </div>
          </div>
        ) : (
          /* ── Parameters read-only ── */
          <div className="space-y-5">
            {/* Execution section */}
            <div>
              <div className="flex items-center gap-2 mb-3">
                <Bot className="h-3.5 w-3.5 text-filigran-ia-primary" />
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Execution")}</p>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <ConfigCard icon={Tag} iconColor="text-feedback-info-primary" label={t_i18n("AI-generated label")} value={assignment.ai_label ? (assignment.ai_label_prompt ? t_i18n("On (custom)") : t_i18n("On")) : t_i18n("Off")} isBoolean active={assignment.ai_label} />
                <ConfigCard
                  icon={HandMetal}
                  iconColor="text-feedback-warning-primary"
                  label={t_i18n("Human in the loop")}
                  value={
                    assignment.human_in_the_loop === null
                      ? t_i18n("Inherit from agent ({state})", { state: agent?.human_in_the_loop ? t_i18n("On") : t_i18n("Off") })
                      : assignment.human_in_the_loop
                        ? (assignment.human_in_the_loop_prompt ? t_i18n("On (custom)") : t_i18n("On"))
                        : t_i18n("Off")
                  }
                  isBoolean
                  active={assignment.human_in_the_loop ?? agent?.human_in_the_loop ?? false}
                />
              </div>
            </div>

            <div className="border-t border-elevation-subtle-soft" />

            {/* Knowledge bases section */}
            <div>
              <div className="flex items-center gap-2 mb-3">
                <BookOpen className="h-3.5 w-3.5 text-feedback-success-primary" />
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Knowledge bases")}</p>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <ConfigCard
                  icon={BookOpen}
                  iconColor="text-feedback-success-primary"
                  label={t_i18n("Read (RAG)")}
                  value={
                    assignment.read_knowledge_base_ids && assignment.read_knowledge_base_ids.length > 0
                      ? assignment.read_knowledge_base_ids.map((kid) => agentKbs.find((kb) => kb.id === kid)?.name ?? t_i18n("Unknown")).join(", ")
                      : t_i18n("All agent KBs")
                  }
                />
                <ConfigCard
                  icon={Database}
                  iconColor="text-accent"
                  label={t_i18n("Write")}
                  value={
                    assignment.write_knowledge_base_ids == null
                      ? (writableKbs.length > 0 ? t_i18n("All writable KBs") : t_i18n("None"))
                      : (() => {
                          const effective = assignment.write_knowledge_base_ids.filter((kid) => writableIdSet.has(kid));
                          return effective.length > 0
                            ? effective.map((kid) => agentKbs.find((kb) => kb.id === kid)?.name ?? t_i18n("Unknown")).join(", ")
                            : t_i18n("None");
                        })()
                  }
                  isBoolean
                  active={
                    assignment.write_knowledge_base_ids == null
                      ? writableKbs.length > 0
                      : assignment.write_knowledge_base_ids.some((kid) => writableIdSet.has(kid))
                  }
                />
              </div>
            </div>

            {showRunners && (
              <>
                <div className="border-t border-elevation-subtle-soft" />

                {/* Runners (native OS-control) section */}
                <div>
                  <div className="flex items-center gap-2 mb-3">
                    <MonitorCog className="h-3.5 w-3.5 text-accent" />
                    <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Runners (computer use)")}</p>
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <ConfigCard
                      icon={MonitorCog}
                      iconColor="text-accent"
                      label={t_i18n("Runners")}
                      value={
                        assignment.runner_ids == null
                          ? (agentRunners.length > 0 ? t_i18n("All agent runners") : t_i18n("None"))
                          : (() => {
                              const effective = assignment.runner_ids.filter((rid) => agentRunnerIdSet.has(rid));
                              return effective.length > 0
                                ? effective.map((rid) => agentRunners.find((r) => r.id === rid)?.name ?? t_i18n("Unknown")).join(", ")
                                : t_i18n("None");
                            })()
                      }
                      isBoolean
                      active={
                        assignment.runner_ids == null
                          ? agentRunners.length > 0
                          : assignment.runner_ids.some((rid) => agentRunnerIdSet.has(rid))
                      }
                    />
                  </div>
                </div>
              </>
            )}

            <div className="border-t border-elevation-subtle-soft" />

            {/* Output section */}
            <div>
              <div className="flex items-center gap-2 mb-3">
                <Bell className="h-3.5 w-3.5 text-feedback-warning-primary" />
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Output visibility")}</p>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <ConfigCard icon={Bell} iconColor="text-feedback-warning-primary" label={t_i18n("Notification")} value={assignment.notify_on_complete ? t_i18n("On") : t_i18n("Off")} isBoolean active={assignment.notify_on_complete} />
                <ConfigCard icon={BarChart3} iconColor="text-feedback-info-primary" label={t_i18n("Latest results")} value={assignment.show_in_digest ? t_i18n("On") : t_i18n("Off")} isBoolean active={assignment.show_in_digest} />
              </div>
            </div>

            <div className="border-t border-elevation-subtle-soft" />

            {/* Safety section */}
            <div>
              <div className="flex items-center gap-2 mb-3">
                <Hourglass className="h-3.5 w-3.5 text-feedback-error-primary" />
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Safety & reliability")}</p>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <ConfigCard
                  icon={Repeat}
                  iconColor="text-filigran-ia-primary"
                  label={t_i18n("Max iterations")}
                  value={
                    assignment.max_iterations != null
                      ? String(clampMaxIterations(assignment.max_iterations))
                      : t_i18n("Inherited ({value})", { value: inheritedMaxIterations })
                  }
                />
                <ConfigCard
                  icon={Building2}
                  iconColor="text-accent"
                  label={t_i18n("Organization context")}
                  value={`${t_i18n(ORGANIZATION_CONTEXT_MODE_LABELS[assignment.organization_context_mode ?? "inherit"])} (${organizationContextInEffect})`}
                />
                <ConfigCard icon={Hourglass} iconColor="text-feedback-error-primary" label={t_i18n("Timeout")} value={assignment.timeout_seconds ? `${assignment.timeout_seconds}s` : t_i18n("No limit")} />
                <ConfigCard icon={RefreshCw} iconColor="text-accent" label={t_i18n("Retry on failure")} value={assignment.retry_on_failure ? t_i18n("On (max {count})", { count: assignment.max_retries }) : t_i18n("Off")} isBoolean active={assignment.retry_on_failure} />
                <ConfigCard icon={FlaskConical} iconColor="text-feedback-warning-primary" label={t_i18n("Dry run")} value={assignment.dry_run ? t_i18n("On") : t_i18n("Off")} isBoolean active={assignment.dry_run} activeTone="amber" />
              </div>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
