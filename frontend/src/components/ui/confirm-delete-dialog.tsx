// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import { Icon } from "@filigran/design-system";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

// ---------------------------------------------------------------------------
// ConfirmDeleteDialog — destructive confirmation with name-based validation.
// Requires the user to type the entity name exactly (case-sensitive) before
// the delete button becomes enabled.  Reusable for any shared entity.
// ---------------------------------------------------------------------------

interface ConfirmDeleteDialogProps {
  /** Whether the dialog is open. */
  open: boolean;
  /** Callback to toggle the dialog open/closed. */
  onOpenChange: (open: boolean) => void;
  /** The exact name the user must type to confirm. */
  name: string;
  /** Called when the user confirms the deletion. */
  onConfirm: () => void;
  /** Dialog title. */
  title?: string;
  /** Descriptive text shown below the title. */
  description?: string;
  /** Label for the confirm button. */
  confirmLabel?: string;
  /** Label for the cancel button. */
  cancelLabel?: string;
}

export function ConfirmDeleteDialog({
  open,
  onOpenChange,
  name,
  onConfirm,
  title = "Delete Shared Resource",
  description,
  confirmLabel = "Delete",
  cancelLabel = "Cancel",
}: ConfirmDeleteDialogProps) {
  const [value, setValue] = useState("");

  const isMatch = name.length > 0 && value === name;

  const handleOpenChange = (next: boolean) => {
    if (!next) setValue("");
    onOpenChange(next);
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-feedback-error-primary/10">
              <Icon name="triangle-alert" size={20} className="text-feedback-error-primary" />
            </div>
            <div className="space-y-1">
              <DialogTitle className="text-base">{title}</DialogTitle>
              <DialogDescription className="content-base leading-relaxed">
                {description ?? (
                  <>
                    You are about to delete{" "}
                    <strong className="text-default-primary">&quot;{name}&quot;</strong>.
                    This resource may be used by other team members.
                  </>
                )}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="space-y-2 pt-1">
          <label htmlFor="confirm-delete-name" className="content-compact text-default-secondary">
            To confirm, type <strong className="text-default-primary">{name}</strong> below:
          </label>
          <Input
            id="confirm-delete-name"
            aria-label={`Type ${name} to confirm deletion`}
            autoFocus
            placeholder={name}
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" size="sm" onClick={() => handleOpenChange(false)}>
            {cancelLabel}
          </Button>
          <Button
            size="sm"
            className="bg-feedback-error-primary hover:bg-feedback-error-primary/90 text-negative-primary border-0"
            disabled={!isMatch}
            onClick={() => {
              onConfirm();
              handleOpenChange(false);
            }}
          >
            {confirmLabel}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
