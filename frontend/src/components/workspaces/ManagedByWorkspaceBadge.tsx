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
 * "Managed by workspace X": a knowledge base a workspace owns - its
 * Sources - is read-only everywhere else and changed from its workspace.
 * `link` is off where the badge sits inside another link (a list card).
 */
import { workspacePath } from "@/lib/task-routes";
import { FolderKanban } from "lucide-react";
import { Link } from "react-router-dom";

import { Badge, BadgeFocusTrigger } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";

export function ManagedByWorkspaceBadge({
  workspace,
  link = false,
  size = "sm",
}: {
  workspace: { id: string; name: string };
  link?: boolean;
  size?: "sm" | "default";
}) {
  const { t_i18n } = useFormatter();
  const managedBy = t_i18n("Managed by workspace {name}", { name: workspace.name });
  // A list card has no room for the name: it goes to the tooltip.
  const label = size === "sm" ? t_i18n("Workspace") : managedBy;
  const badge = (
    <Badge
      variant="cyan"
      aria-label={managedBy}
      className={size === "sm" ? "gap-0.5 text-[9px]" : "max-w-[20rem] gap-1 content-caption"}
    >
      <FolderKanban className={size === "sm" ? "h-2.5 w-2.5 shrink-0" : "h-3 w-3 shrink-0"} />
      <span className="truncate">{label}</span>
    </Badge>
  );
  return (
    <Tip content={`${managedBy}. ${t_i18n("Read-only here: its documents and sources are changed from the workspace.")}`}>
      {link ? (
        <Link to={workspacePath(workspace.id)} className="rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus">
          {badge}
        </Link>
      ) : (
        <BadgeFocusTrigger>{badge}</BadgeFocusTrigger>
      )}
    </Tip>
  );
}
