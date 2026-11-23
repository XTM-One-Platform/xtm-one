// SPDX-License-Identifier: Apache-2.0

import { useState, type ReactNode } from "react";
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
  /** Extra content between the description and the name field (what the action changes). */
  children?: ReactNode;
  /** Label above the name field; default "To confirm, type <name> below:". */
  inputLabel?: ReactNode;
  /** Accessible name of the name field; default "Type <name> to confirm deletion". */
  inputAriaLabel?: string;
  /** Keeps the confirm button disabled even once the name matches (a plan loading, nothing to do). */
  confirmDisabled?: boolean;
  /** The confirmed action is running: both buttons are disabled and the dialog stays open. */
  pending?: boolean;
  /** Why the action failed, shown inline under the field (role="alert"), never in a toast alone. */
  error?: ReactNode;
  /** false: an async action, the caller closes the dialog once it succeeded. Default true. */
  closeOnConfirm?: boolean;
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
  children,
  inputLabel,
  inputAriaLabel,
  confirmDisabled = false,
  pending = false,
  error,
  closeOnConfirm = true,
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

        {children}

        <div className="space-y-2 pt-1">
          <label htmlFor="confirm-delete-name" className="content-compact text-default-secondary">
            {inputLabel ?? (
              <>
                To confirm, type <strong className="text-default-primary">{name}</strong> below:
              </>
            )}
          </label>
          <Input
            id="confirm-delete-name"
            aria-label={inputAriaLabel ?? `Type ${name} to confirm deletion`}
            autoComplete="off"
            autoFocus
            placeholder={name}
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          {error ? (
            <p role="alert" className="content-compact text-feedback-error-tertiary" data-testid="confirm-delete-error">
              {error}
            </p>
          ) : null}
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" size="sm" disabled={pending} onClick={() => handleOpenChange(false)}>
            {cancelLabel}
          </Button>
          <Button
            size="sm"
            className="bg-feedback-error-primary hover:bg-feedback-error-primary/90 text-negative-primary border-0"
            disabled={!isMatch || confirmDisabled || pending}
            onClick={() => {
              onConfirm();
              if (closeOnConfirm) handleOpenChange(false);
            }}
          >
            {confirmLabel}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
