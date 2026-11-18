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

import { useState, useRef } from "react";
import { Terminal, Filter, ChevronDown, Wrench } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { PromptGenerator } from "@/components/ui/prompt-generator";
import { VariableInsertButton } from "@/components/ui/variable-insert-button";
import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { useUpdateAssignment } from "@/hooks/api/useAssignmentApi";
import { useToast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import type { ActivationCheck, ActivationCheckCondition, Assignment } from "@/types";

type CheckMode = "non_empty" | "empty" | "path";
type PathOperator = "in" | "not_in" | "contains";

/** The condition of a tool check as one line ("$.status in [...]"). */
function describeCondition(condition: ActivationCheckCondition): string {
  if (typeof condition === "string") return condition.replace("_", "-");
  const operator = (["in", "not_in", "contains"] as const).find((key) => key in condition) ?? "in";
  return `${condition.path} ${operator.replace("_", " ")} ${JSON.stringify(condition[operator])}`;
}
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { CardEditActions } from "@/components/ui/card-edit-actions";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";

// ---------------------------------------------------------------------------
// PromptCard — Gate prompt + Action prompt (edit + read-only)
//
// Renders the full prompt section:
//   • Standalone gate-prompt card above the grid (read-only, when not editing)
//   • A 50/50 grid with the action prompt on the left and `renderRight` on the right
// ---------------------------------------------------------------------------

interface PromptCardProps {
  assignment: Assignment;
  canManage: boolean;
  onUpdate: () => void;
  /** Content rendered in the right column of the 50/50 grid (e.g. IntegrationBindingsCard). */
  renderRight?: React.ReactNode;
}

export function PromptCard({ assignment, canManage, onUpdate, renderRight }: PromptCardProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateAssignment();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  const aiOff = useAiActionDisabled();

  const activationRef = useRef<HTMLTextAreaElement>(null);
  const actionRef = useRef<HTMLTextAreaElement>(null);
  const [editing, setEditing] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [activationPrompt, setActivationPrompt] = useState("");
  const [promptExpanded, setPromptExpanded] = useState(false);
  const [gateExpanded, setGateExpanded] = useState(false);
  // Tool-check gate, evaluated by the platform before the gate prompt.
  const [checkTool, setCheckTool] = useState("");
  const [checkArguments, setCheckArguments] = useState("");
  const [checkMode, setCheckMode] = useState<CheckMode>("non_empty");
  const [checkPath, setCheckPath] = useState("");
  const [checkOperator, setCheckOperator] = useState<PathOperator>("in");
  const [checkValues, setCheckValues] = useState("");

  const startEditing = () => {
    setPrompt(assignment.prompt ?? "");
    setActivationPrompt(assignment.activation_prompt ?? "");
    const check = assignment.activation_check;
    setCheckTool(check?.tool ?? "");
    setCheckArguments(check && Object.keys(check.arguments ?? {}).length ? JSON.stringify(check.arguments, null, 2) : "");
    const condition = check?.condition ?? "non_empty";
    if (typeof condition === "string") {
      setCheckMode(condition);
      setCheckPath("");
      setCheckValues("");
    } else {
      const operator = (["in", "not_in", "contains"] as const).find((key) => key in condition) ?? "in";
      setCheckMode("path");
      setCheckPath(condition.path);
      setCheckOperator(operator);
      const value = condition[operator];
      setCheckValues(Array.isArray(value) ? value.map(String).join(", ") : String(value ?? ""));
    }
    setEditing(true);
  };

  /** The check to save, `null` to remove it, or `undefined` when its arguments are not a JSON object. */
  const pendingCheck = (): ActivationCheck | null | undefined => {
    if (!checkTool.trim()) return null;
    let args: Record<string, unknown> = {};
    if (checkArguments.trim()) {
      try {
        const parsed: unknown = JSON.parse(checkArguments);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return undefined;
        args = parsed as Record<string, unknown>;
      } catch {
        return undefined;
      }
    }
    const condition: ActivationCheckCondition =
      checkMode !== "path"
        ? checkMode
        : checkOperator === "contains"
          ? { path: checkPath.trim(), contains: checkValues.trim() }
          : { path: checkPath.trim(), [checkOperator]: checkValues.split(",").map((v) => v.trim()).filter(Boolean) };
    // The card edits the first check only: the checks it lists (`also`, `or`) and
    // its integration (while the tool is unchanged) are kept as stored, never
    // dropped by a save.
    const stored = assignment.activation_check;
    const tool = checkTool.trim();
    return {
      tool,
      arguments: args,
      condition,
      ...(stored?.integration_id && stored.tool === tool ? { integration_id: stored.integration_id } : {}),
      ...(stored?.also?.length ? { also: stored.also } : {}),
      ...(stored?.or?.length ? { or: stored.or } : {}),
    };
  };

  const handleSave = async () => {
    const activationCheck = pendingCheck();
    if (activationCheck === undefined) {
      toast({ title: t_i18n("The tool check arguments must be a JSON object"), variant: "error" });
      return;
    }
    // A variable the prompt references resolves in the assignment's runs.
    if (!(await confirmSharing({ entity_type: "assignment", entity_id: assignment.id, change: { prompt, activation_prompt: activationPrompt } }))) return;
    update.mutate(
      {
        id: assignment.id,
        prompt,
        // Send an explicit `null` (not `undefined`, which JSON.stringify drops
        // and the API reads as "unchanged") so emptying the field actually
        // removes the gate. A whitespace-only value clears it too.
        // The trim only decides clear-vs-keep: a real gate is sent verbatim.
        activation_prompt: activationPrompt.trim() ? activationPrompt : null,
        activation_check: activationCheck,
      },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Prompt updated"), variant: "success" });
          setEditing(false);
          onUpdate();
        },
      },
    );
  };

  return (
    <>
      {consentElement}
      {/* Tool check (standalone, the first gate stage) */}
      {assignment.activation_check && !editing && (
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-2">
            <CardTitle className="content-base flex items-center gap-2"><Wrench className="h-4 w-4 text-feedback-warning-primary" /> {t_i18n("Tool Check")}</CardTitle>
          </CardHeader>
          <CardContent className="px-4 pb-4 pt-0">
            <div className="bg-elevation-hover rounded-sm p-4 space-y-1">
              <code className="block content-compact font-mono-plex text-accent break-all">
                {assignment.activation_check.tool}({JSON.stringify(assignment.activation_check.arguments ?? {})})
              </code>
              <p className="content-compact text-default-secondary">
                {t_i18n("Runs when: {condition}", { condition: describeCondition(assignment.activation_check.condition) })}
              </p>
              <p className="content-caption text-default-secondary">
                {t_i18n("Called by the platform with no model call, before the gate prompt. A run whose check fails is skipped with no tokens spent.")}
              </p>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Gate prompt (standalone, above the 50/50 grid) */}
      {assignment.activation_prompt?.trim() && !editing && (
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-2">
            <CardTitle className="content-base flex items-center gap-2"><Filter className="h-4 w-4 text-feedback-warning-primary" /> {t_i18n("Gate Prompt")}</CardTitle>
          </CardHeader>
          <CardContent className="px-4 pb-4 pt-0">
            <div className="bg-elevation-hover rounded-sm p-4">
              <div className={cn("content-base overflow-hidden", !gateExpanded && "max-h-[6em]")}>
                <ChatMarkdown content={assignment.activation_prompt!} className="content-base text-default-secondary [&_p]:content-base [&_p]:leading-relaxed [&_p]:mb-1.5 [&_p:last-child]:mb-0 [&_li]:content-base [&_h1]:content-base [&_h2]:content-base [&_h3]:content-base" />
              </div>
              {!gateExpanded && (
                <div className="h-6 -mt-6 relative bg-gradient-to-t from-[var(--bg-elevation-hover)] to-transparent pointer-events-none" />
              )}
              <button
                onClick={() => setGateExpanded((v) => !v)}
                className="flex items-center gap-1 content-caption text-default-secondary hover:text-default-primary transition-colors mt-1"
              >
                {gateExpanded ? t_i18n("Collapse") : t_i18n("Expand")}
                <ChevronDown className={cn("h-3 w-3 transition-transform", gateExpanded && "rotate-180")} />
              </button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* 50/50 grid: Action Prompt | renderRight */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* LEFT — Action Prompt */}
        <Card className="flex flex-col">
          <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-2">
            <CardTitle className="content-base flex items-center gap-2"><Terminal className="h-4 w-4 text-feedback-success-primary" /> {t_i18n("Action Prompt")}</CardTitle>
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
          <CardContent className="px-4 pb-4 pt-0 flex-1">
            {editing ? (
              <div className="space-y-4">
                <div className="space-y-1.5">
                  <label className="content-base text-default-secondary flex items-center gap-1.5">
                    <Wrench className="h-3.5 w-3.5 text-feedback-warning-primary" /> {t_i18n("Tool check")} <span className="content-compact italic">{t_i18n("(optional)")}</span>
                  </label>
                  <p className="content-caption text-default-secondary">
                    {t_i18n("Called by the platform with no model call, before the gate prompt. A run whose check fails is skipped with no tokens spent.")}
                  </p>
                  <Input
                    aria-label={t_i18n("Tool")}
                    className="[&_input]:font-mono-plex"
                    placeholder={t_i18n("Read-only tool, e.g. list_work_items")}
                    value={checkTool}
                    onChange={(e) => setCheckTool(e.target.value)}
                  />
                  {checkTool.trim() && (
                    <div className="space-y-1.5">
                      <textarea
                        aria-label={t_i18n("Tool arguments (JSON)")}
                        className={`flex min-h-[60px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base font-mono-plex placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
                        placeholder='{"owner_agent_id": "{{agent.id}}", "open_only": true}'
                        value={checkArguments}
                        onChange={(e) => setCheckArguments(e.target.value)}
                      />
                      <div className="flex flex-wrap items-center gap-2">
                        <Select value={checkMode} onValueChange={(v) => setCheckMode(v as CheckMode)}>
                          <SelectTrigger className="w-56" aria-label={t_i18n("Tool check")}><SelectValue /></SelectTrigger>
                          <SelectContent>
                            <SelectItem value="non_empty">{t_i18n("Runs when the result is not empty")}</SelectItem>
                            <SelectItem value="empty">{t_i18n("Runs when the result is empty")}</SelectItem>
                            <SelectItem value="path">{t_i18n("Runs when a field matches")}</SelectItem>
                          </SelectContent>
                        </Select>
                        {checkMode === "path" && (
                          <>
                            <Input aria-label={t_i18n("Field path")} className="w-32 [&_input]:font-mono-plex" placeholder="$.status" value={checkPath} onChange={(e) => setCheckPath(e.target.value)} />
                            <Select value={checkOperator} onValueChange={(v) => setCheckOperator(v as PathOperator)}>
                              <SelectTrigger className="w-36" aria-label={t_i18n("Operator")}><SelectValue /></SelectTrigger>
                              <SelectContent>
                                <SelectItem value="in">{t_i18n("is one of")}</SelectItem>
                                <SelectItem value="not_in">{t_i18n("is none of")}</SelectItem>
                                <SelectItem value="contains">{t_i18n("contains")}</SelectItem>
                              </SelectContent>
                            </Select>
                            <Input aria-label={t_i18n("Values")} className="flex-1 min-w-32" placeholder={t_i18n("e.g. open, blocked")} value={checkValues} onChange={(e) => setCheckValues(e.target.value)} />
                          </>
                        )}
                      </div>
                    </div>
                  )}
                </div>
                <div className="space-y-1.5">
                  <div className="flex items-center justify-between">
                    <label className="content-base text-default-secondary flex items-center gap-1.5">
                      <Filter className="h-3.5 w-3.5 text-feedback-warning-primary" /> {t_i18n("Gate prompt")} <span className="content-compact italic">{t_i18n("(optional)")}</span>
                    </label>
                    <div className="flex items-center gap-2">
                      <VariableInsertButton textareaRef={activationRef} value={activationPrompt} onChange={setActivationPrompt} />
                      <PromptGenerator contextType="gate_prompt" currentText={activationPrompt} onGenerated={setActivationPrompt} assignmentName={assignment.name} />
                    </div>
                  </div>
                  <textarea
                    ref={activationRef}
                    className={`flex min-h-[60px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
                    placeholder={t_i18n('e.g. "Is this a question I should answer? YES or NO."')}
                    value={activationPrompt}
                    onChange={(e) => setActivationPrompt(e.target.value)}
                  />
                </div>
                <div className="space-y-1.5">
                  <div className="flex items-center justify-between">
                    <label className="content-base text-default-secondary flex items-center gap-1.5">
                      <Terminal className="h-3.5 w-3.5 text-feedback-success-primary" /> {t_i18n("Action prompt")}
                    </label>
                    <div className="flex items-center gap-2">
                      <VariableInsertButton textareaRef={actionRef} value={prompt} onChange={setPrompt} />
                      <PromptGenerator contextType="assignment_prompt" currentText={prompt} onGenerated={setPrompt} assignmentName={assignment.name} />
                    </div>
                  </div>
                  <textarea
                    ref={actionRef}
                    className={`flex min-h-[200px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
                    value={prompt}
                    onChange={(e) => setPrompt(e.target.value)}
                  />
                </div>
              </div>
            ) : (
              <div className="bg-elevation-hover rounded-sm p-4">
                <div className={cn("content-base overflow-hidden", !promptExpanded && "max-h-[10em]")}>
                  <ChatMarkdown content={assignment.prompt ?? ""} className="content-base text-default-secondary [&_p]:content-base [&_p]:leading-relaxed [&_p]:mb-1.5 [&_p:last-child]:mb-0 [&_li]:content-base [&_h1]:content-base [&_h2]:content-base [&_h3]:content-base" />
                </div>
                {!promptExpanded && (
                  <div className="h-6 -mt-6 relative bg-gradient-to-t from-[var(--bg-elevation-hover)] to-transparent pointer-events-none" />
                )}
                <button
                  onClick={() => setPromptExpanded((v) => !v)}
                  className="flex items-center gap-1 content-caption text-default-secondary hover:text-default-primary transition-colors mt-1"
                >
                  {promptExpanded ? t_i18n("Collapse") : t_i18n("Expand")}
                  <ChevronDown className={cn("h-3 w-3 transition-transform", promptExpanded && "rotate-180")} />
                </button>
              </div>
            )}
          </CardContent>
        </Card>

        {/* RIGHT — passed via renderRight */}
        {renderRight}
      </div>
    </>
  );
}
