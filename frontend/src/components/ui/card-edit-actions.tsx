// SPDX-License-Identifier: Apache-2.0

import { Icon } from "@filigran/design-system";

import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";

/**
 * The Edit / Cancel / Save actions of a card header that switches between a
 * read view and an edit view (agent Configuration cards, assignment cards,
 * Settings cards). One pattern everywhere (#9375, Design standards s.2 and s.9):
 * every button at the default 36 px height, Edit and Cancel secondary,
 * Cancel first and Save, the primary, last at the right end, 16 px
 * design-system icons.
 *
 * `canEdit` false renders nothing in the read view (the viewer may not edit).
 * `aiGated` wraps Save in the shared "AI is off" explanation: pass it when the
 * save is refused while AI is off (`saveDisabled` carries the refusal itself).
 */
export interface CardEditActionsProps {
  editing: boolean;
  onEdit: () => void;
  onCancel: () => void;
  onSave: () => void;
  canEdit?: boolean;
  saveDisabled?: boolean;
  aiGated?: boolean;
  testIds?: { edit?: string; cancel?: string; save?: string };
}

export function CardEditActions({
  editing,
  onEdit,
  onCancel,
  onSave,
  canEdit = true,
  saveDisabled = false,
  aiGated = false,
  testIds,
}: CardEditActionsProps) {
  const { t_i18n } = useFormatter();
  if (!editing) {
    if (!canEdit) return null;
    return (
      <Button variant="secondary" className="gap-1.5 shrink-0" onClick={onEdit} data-testid={testIds?.edit}>
        <Icon name="pencil" size={16} aria-hidden /> {t_i18n("Edit")}
      </Button>
    );
  }
  const save = (
    <Button className="gap-1.5" onClick={onSave} disabled={saveDisabled} data-testid={testIds?.save}>
      <Icon name="save" size={16} aria-hidden /> {t_i18n("Save")}
    </Button>
  );
  return (
    <div className="flex gap-2 shrink-0" data-testid="card-edit-actions">
      <Button variant="secondary" onClick={onCancel} data-testid={testIds?.cancel}>
        {t_i18n("Cancel")}
      </Button>
      {aiGated ? <AiDisabledTip>{save}</AiDisabledTip> : save}
    </div>
  );
}
