// SPDX-License-Identifier: Apache-2.0

/**
 * The Delete action of an inventory page header (#1729): the agent page's destructive icon button (trash, tertiary)
 * with a confirmation, for an asset, an asset group or a software entered by hand. A synced entry never shows it: its
 * source owns it and would bring it back on the next sync.
 */
import { useState } from "react";
import { Icon, IconButton, Spinner } from "@filigran/design-system";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";

interface InventoryDeleteActionProps {
  /** The entry's name, said in the confirmation. */
  name: string;
  /** The confirmation title, already translated. */
  title: string;
  /** What the delete changes, already translated. */
  description: string;
  pending: boolean;
  onConfirm: () => void;
  testId: string;
}

export function InventoryDeleteAction({ name, title, description, pending, onConfirm, testId }: InventoryDeleteActionProps) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  return (
    <>
      <Tip content={t_i18n("Delete")}>
        <IconButton
          variant="destructive"
          priority="tertiary"
          aria-label={t_i18n("Delete {name}", { name })}
          icon={pending ? <Spinner size="sm" tone="inherit" label={t_i18n("Loading...")} /> : <Icon name="trash-2" size={16} aria-hidden />}
          aria-busy={pending || undefined}
          className="h-8 w-8"
          disabled={pending}
          onClick={() => setOpen(true)}
          data-testid={testId}
        />
      </Tip>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        variant="danger"
        title={title}
        description={description}
        confirmLabel={t_i18n("Delete")}
        onConfirm={onConfirm}
      />
    </>
  );
}
