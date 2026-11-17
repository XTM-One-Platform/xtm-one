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
 * A button of a block in the chat transcript that hands a prompt to the
 * conversation's agent ("Send to OpenCTI", "Generate executive summary").
 *
 * It is enabled only when the chat around it can send (`useChatActions`). The
 * one wait that is not the user's to fix, the conversation's tools still
 * resolving, explains itself: a spinning icon, `aria-busy` and the
 * "Loading tools…" tooltip until the tools are known, then the button's own
 * hint again. The tooltip wrapper is a tab stop while the button is disabled,
 * so the reason reaches the keyboard too.
 */
import type { ReactNode } from "react";
import { Check } from "lucide-react";

import { useChatActions } from "@/components/chat/chat-actions";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { Spinner } from "@filigran/design-system";

interface BlockPromptButtonProps {
  /** What the button does, shown as its tooltip once it can send. */
  tip: string;
  /** The label before it is pressed. */
  label: string;
  /** The label once pressed. */
  requestedLabel: string;
  /** The icon before it is pressed (after: a check). */
  icon: ReactNode;
  /** Whether it was pressed: it then stays disabled, showing `requestedLabel`. */
  requested: boolean;
  onClick: () => void;
  variant?: "default" | "outline";
  /** `data-investigation-action` of the button. */
  action: string;
}

export function BlockPromptButton({
  tip,
  label,
  requestedLabel,
  icon,
  requested,
  onClick,
  variant = "default",
  action,
}: BlockPromptButtonProps) {
  const { t_i18n } = useFormatter();
  const chat = useChatActions();
  if (!chat) return null;
  const resolving = !requested && !chat.canSend && !!chat.toolsResolving;
  return (
    <Tip content={resolving ? t_i18n("Loading tools…") : tip} wrap="auto">
      <Button
        size="sm"
        variant={variant}
        disabled={requested || !chat.canSend}
        aria-busy={resolving || undefined}
        onClick={onClick}
        className="h-7 gap-1.5 content-compact"
        data-investigation-action={action}
      >
        {requested ? (
          <Check className="h-3.5 w-3.5" />
        ) : resolving ? (
          <Spinner size="sm" tone="inherit" label={t_i18n("Loading...")} />
        ) : (
          icon
        )}
        {requested ? requestedLabel : label}
      </Button>
    </Tip>
  );
}
