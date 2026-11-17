// SPDX-License-Identifier: Apache-2.0

import type { ComponentProps, TouchEvent, WheelEvent } from "react";
import { ComboboxContent } from "@filigran/design-system";

import { useInModalDialog } from "@/components/ui/modal-dialog-context";

/**
 * The Combobox list, scrollable inside a modal dialog: portalled out of the
 * dialog, its wheel and touch moves would reach the dialog's scroll lock and be
 * cancelled. They stop at the list there, and go on as usual elsewhere.
 */
export function ScrollableComboboxContent({ onWheel, onTouchMove, ...props }: ComponentProps<typeof ComboboxContent>) {
  const inDialog = useInModalDialog();
  return (
    <ComboboxContent
      {...props}
      onWheel={(event: WheelEvent<HTMLDivElement>) => {
        onWheel?.(event);
        if (inDialog) event.stopPropagation();
      }}
      onTouchMove={(event: TouchEvent<HTMLDivElement>) => {
        onTouchMove?.(event);
        if (inDialog) event.stopPropagation();
      }}
    />
  );
}
