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
 * Filing a conversation into a workspace other people can see asks first
 * (spec 032 FR-022): its members can then search the conversation.
 * Shared by the file dialog and the chat sidebar's drag and drop.
 */
import type { ReactNode } from "react";

import { useConfirm } from "@/components/ui/confirm-dialog";
import { useFormatter } from "@/i18n";
import { workspaceReachesOthers } from "@/lib/workspace-sharing";
import type { Workspace } from "@/types";

/**
 * The FR-022 question before filing into a workspace other people can see:
 * `[element to render, ask(workspace, userId)]`, the second resolving `true`
 * at once for a workspace only the person sees.
 */
export function useConfirmSharedFiling(): [
  ReactNode,
  (workspace: Workspace, userId: string | undefined) => Promise<boolean>,
] {
  const { t_i18n } = useFormatter();
  const [ConfirmEl, confirm] = useConfirm();
  const ask = (workspace: Workspace, userId: string | undefined) =>
    workspaceReachesOthers(workspace, userId)
      ? confirm({
          title: t_i18n("File into a shared workspace?"),
          description: t_i18n(
            "{name} is shared with other people. Once this conversation is filed there, they can search what was written in it.",
            { name: workspace.name },
          ),
          confirmLabel: t_i18n("File it"),
          variant: "warning",
        })
      : Promise.resolve(true);
  return [ConfirmEl, ask];
}
