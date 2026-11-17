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
 * The conversation menu of the /chat page (the panel's `headerActions`): what
 * a conversation offers beside the panel's own controls, as data.
 */
import {
  ArrowRightLeft,
  FileDown,
  FileText,
  FolderKanban,
  FolderOpen,
  Globe,
  Layers,
  MessagesSquare,
  UserPlus,
} from "lucide-react";
import type { OverflowMenuItem } from "@/components/ui/overflow-menu";

interface ConversationMenuState {
  conversationId: string | null;
  /** The workspace the conversation is filed in. */
  workspaceId: string | null;
  isReadOnly: boolean;
  isChannelOnly: boolean;
  /** A web conversation: a Slack or Teams thread is answered by its channel's bot. */
  isWebConversation: boolean;
  /** A turn is running in it. */
  turnRunning: boolean;
  isOrgReadable: boolean;
  agenticLicensed: boolean;
}

interface ConversationMenuActions {
  showFiles: () => void;
  share: () => void;
  continueInChannel: () => void;
  continueWithAgent: () => void;
  exportAs: (format: "markdown" | "pdf") => void;
  fileInWorkspace: () => void;
  showWorkspaceKnowledge: () => void;
}

export function conversationMenuItems(
  state: ConversationMenuState,
  actions: ConversationMenuActions,
  t_i18n: (key: string) => string,
): OverflowMenuItem[] {
  const { conversationId, workspaceId, isReadOnly, isChannelOnly, isWebConversation, turnRunning, isOrgReadable, agenticLicensed } =
    state;
  return [
    {
      id: "conversation-files",
      label: t_i18n("Conversation files"),
      icon: FolderOpen,
      disabled: !conversationId,
      onSelect: actions.showFiles,
    },
    {
      id: "share-conversation",
      label: t_i18n("Share"),
      icon: isOrgReadable ? Globe : UserPlus,
      disabled: !conversationId || isReadOnly,
      onSelect: actions.share,
    },
    {
      id: "continue-in-channel",
      label: t_i18n("Continue in Slack or Teams"),
      icon: MessagesSquare,
      disabled: !conversationId || isReadOnly || isChannelOnly,
      onSelect: actions.continueInChannel,
    },
    {
      // The owner's web conversation, between two turns.
      id: "continue-with-agent",
      label: t_i18n("Continue with another agent"),
      icon: ArrowRightLeft,
      disabled: !conversationId || isReadOnly || !isWebConversation || turnRunning,
      onSelect: actions.continueWithAgent,
    },
    {
      id: "export-markdown",
      label: t_i18n("Export as Markdown"),
      icon: FileText,
      disabled: !conversationId,
      onSelect: () => actions.exportAs("markdown"),
    },
    {
      id: "export-pdf",
      label: t_i18n("Export as PDF"),
      icon: FileDown,
      disabled: !conversationId,
      onSelect: () => actions.exportAs("pdf"),
    },
    ...(agenticLicensed
      ? [
          {
            id: "file-conversation",
            label: t_i18n("File in a workspace"),
            icon: Layers,
            disabled: !conversationId || isReadOnly,
            onSelect: actions.fileInWorkspace,
          },
          // What the conversation's workspace gives its agents.
          ...(conversationId && workspaceId
            ? [
                {
                  id: "workspace-knowledge",
                  label: t_i18n("Workspace knowledge"),
                  icon: FolderKanban,
                  onSelect: actions.showWorkspaceKnowledge,
                },
              ]
            : []),
        ]
      : []),
  ];
}
