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
 * The knowledge of the workspace a conversation is filed in, beside
 * the conversation: the same panel as the workspace page, in the slide-over the
 * conversation files use.
 */
import { workspacePath } from "@/lib/task-routes";
import { ExternalLink, FolderKanban, X } from "lucide-react";
import { Link } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { SlideOver } from "@/components/ui/slide-over";
import { KnowledgePanel } from "@/components/workspaces/knowledge/KnowledgePanel";
import { useWorkspace } from "@/hooks/api/useWorkspaceApi";
import { useFormatter } from "@/i18n";

export function WorkspaceKnowledgeDrawer({
  workspaceId,
  open,
  onClose,
}: {
  workspaceId: string | null;
  open: boolean;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const workspace = useWorkspace(open ? workspaceId : null);
  if (!workspaceId) return null;

  return (
    <SlideOver open={open} onClose={onClose} label={t_i18n("Workspace knowledge")} width="w-[460px]">
      <div className="flex items-start gap-3 border-b border-elevation-subtle p-4">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-accent/10">
          <FolderKanban className="h-[18px] w-[18px] text-accent" />
        </div>
        <div className="min-w-0 flex-1">
          <h3 className="content-base font-semibold">{t_i18n("Workspace knowledge")}</h3>
          <p className="mt-0.5 truncate content-caption text-default-secondary">{workspace.data?.name ?? ""}</p>
        </div>
        <Button asChild variant="ghost" size="sm" className="h-8 gap-1.5">
          <Link to={workspacePath(workspaceId)} onClick={onClose}>
            <ExternalLink className="h-3.5 w-3.5" />
            {t_i18n("Open workspace")}
          </Link>
        </Button>
        <Button variant="ghost" size="sm" className="h-8 w-8 p-0" aria-label={t_i18n("Close")} onClick={onClose}>
          <X className="h-4 w-4" />
        </Button>
      </div>
      <div className="flex-1 overflow-y-auto p-4">
        <KnowledgePanel workspaceId={workspaceId} />
      </div>
    </SlideOver>
  );
}
