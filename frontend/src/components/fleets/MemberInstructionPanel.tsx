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
 * The instruction dialog of a connected member (dev-docs/ctem-fleets.md
 * 10.5, R5): the shared centred `Dialog` (no new drawer, design standards s2)
 * with the collaboration instruction editor (4000 characters, the counter
 * under it), "Use the fleet documentation" to prefill it, the shared
 * `PromptGenerator` on its own full-width row for an AI first draft on request
 * (`POST .../instruction-draft`), and the automations folded out of the row at
 * 1024. Neither the prefill nor the draft replaces typed text without asking
 * (A4); nothing is saved until the person saves.
 */
import { useState } from "react";
import { BookOpen, RotateCcw } from "lucide-react";
import { Textarea } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { PromptGenerator } from "@/components/ui/prompt-generator";
import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { useDraftFleetMemberInstruction, useFleetDocumentation, type Fleet, type FleetMember } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import { fleetRefusalMessage } from "./fleet-refusals";
import { INSTRUCTION_MAX, fillDecision, instructionPrefill } from "./instruction-editor";
import { MEMBER_SETTING_LABELS, RESET_SETTING_LABELS, isOverridden } from "./members-view";

const FIELD_ID = "fleet-member-instruction";

export interface MemberInstructionPanelProps {
  fleet: Fleet;
  /** The member the dialog edits; `null` closes it. */
  member: FleetMember | null;
  canEdit: boolean;
  saving: boolean;
  enabledAutomations: number;
  onClose: () => void;
  /** Writes the instruction (`null` resets it to the fleet default); the caller toasts and closes. */
  onSave: (member: FleetMember, instruction: string | null) => void;
}

/** Keyed on the member and its stored text: a change saved elsewhere re-reads the server value. */
export function MemberInstructionPanel(props: MemberInstructionPanelProps) {
  const { t_i18n } = useFormatter();
  const { member, onClose } = props;
  return (
    <Dialog open={member !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle className="min-w-0 truncate">
            {member ? t_i18n("{name} settings", { name: member.name }) : t_i18n("Member settings")}
          </DialogTitle>
        </DialogHeader>
        {member && <InstructionEditor key={`${member.id}:${member.collaboration_instruction}`} {...props} member={member} />}
      </DialogContent>
    </Dialog>
  );
}

function InstructionEditor({
  fleet,
  member,
  canEdit,
  saving,
  enabledAutomations,
  onClose,
  onSave,
}: MemberInstructionPanelProps & { member: FleetMember }) {
  const { t_i18n } = useFormatter();
  const [confirmElement, confirm] = useConfirm();
  const stored = member.collaboration_instruction;
  const [text, setText] = useState(stored);
  const [refusal, setRefusal] = useState<string | null>(null);
  const overridden = isOverridden(member, "collaboration_instruction");
  const draft = useDraftFleetMemberInstruction(fleet.id);
  const { data: documentation } = useFleetDocumentation(fleet.id, canEdit);
  const prefill = instructionPrefill(documentation, member.name, fleet.member_defaults?.collaboration_instruction);
  const dirty = text !== stored;

  /** Fill an empty field at once; ask before replacing typed text (A4). */
  const offer = async (next: string) => {
    if (fillDecision(text, next) === "ask") {
      const ok = await confirm({
        title: t_i18n("Replace the instruction?"),
        description: t_i18n("The text you typed is replaced. Nothing is saved until you save."),
        confirmLabel: t_i18n("Replace"),
        variant: "warning",
      });
      if (!ok) return;
    }
    setText(next.slice(0, INSTRUCTION_MAX));
  };

  /** The `generate` callback of the shared PromptGenerator: the brief goes to the instruction-draft route. */
  const generate = async (brief: string) => {
    setRefusal(null);
    try {
      const { text: drafted } = await draft.mutateAsync({ memberId: member.id, brief, current_text: text.trim() || null });
      return drafted;
    } catch (err) {
      setRefusal(fleetRefusalMessage(err, t_i18n) ?? t_i18n("The instruction was not drafted"));
      throw err;
    }
  };

  return (
    <>
      <div className="space-y-4">
        <SettingsField
          id={FIELD_ID}
          label={t_i18n(MEMBER_SETTING_LABELS.collaboration_instruction)}
          helper={t_i18n("When the fleet calls this member and what it asks; empty inherits the fleet default")}
          info={t_i18n(
            "The orchestrator reads this instruction before it calls the member: when to call it and what to ask. Example: \"Call me for every new critical CVE; ask for the affected assets.\" Empty, the member inherits the fleet default.",
          )}
          error={refusal ?? undefined}
        >
          <Textarea
            id={FIELD_ID}
            value={text}
            rows={10}
            maxLength={INSTRUCTION_MAX}
            readOnly={!canEdit}
            aria-labelledby={settingsFieldLabelId(FIELD_ID)}
            aria-describedby={settingsFieldHintId(FIELD_ID)}
            placeholder={t_i18n("When and how the fleet works with this member (optional)")}
            onChange={(e) => setText(e.target.value)}
          />
        </SettingsField>
        {/* The row wraps: a long reset label (es) goes to its own line instead of running past its button (UX round 2, B1). */}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="shrink-0 content-compact text-default-secondary" data-testid="fleet-member-instruction-count">
            {t_i18n("{count} / {max} characters", { count: text.length, max: INSTRUCTION_MAX })}
          </span>
          {canEdit && overridden && (
            <Button type="button" variant="ghost" size="sm" className="gap-1.5" disabled={saving} onClick={() => onSave(member, null)}>
              <RotateCcw className="h-3.5 w-3.5 shrink-0" aria-hidden />
              {t_i18n(RESET_SETTING_LABELS.collaboration_instruction)}
            </Button>
          )}
        </div>
        {canEdit && (
          <div className="space-y-2">
            <div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="gap-1.5"
                disabled={prefill === null}
                onClick={() => prefill !== null && void offer(prefill)}
              >
                <BookOpen className="h-3.5 w-3.5" aria-hidden />
                {t_i18n("Use the fleet documentation")}
              </Button>
            </div>
            {/* Its own full-width row: the draft brief input gets the dialog's width (UX round 1, B5). */}
            <PromptGenerator
              contextType="fleet_collaboration_instruction"
              currentText={text}
              agentName={member.name}
              generate={generate}
              onGenerated={(next) => void offer(next)}
            />
          </div>
        )}
        <div className="space-y-1">
          <h3 className="content-compact font-medium text-default-secondary">{t_i18n("Automations")}</h3>
          <p className="content-compact">
            {enabledAutomations > 0
              ? t_i18n("{count, plural, one {# enabled} other {# enabled}}", { count: enabledAutomations })
              : t_i18n("None")}
          </p>
        </div>
      </div>
      {canEdit && (
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button type="button" disabled={!dirty || saving} onClick={() => onSave(member, text)}>
            {t_i18n("Save")}
          </Button>
        </div>
      )}
      {confirmElement}
    </>
  );
}
