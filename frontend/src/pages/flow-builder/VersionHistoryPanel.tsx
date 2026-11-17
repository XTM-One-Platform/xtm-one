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
 * Version history.
 *
 * Every save snapshots the graph, so this is the flow's whole editing history
 * up to the retention cap the server reports.
 *
 * Restoring is non-destructive: the server creates a NEW version holding the
 * restored graph, so whatever was current beforehand stays in the list and the
 * restore itself can be undone by restoring that entry.
 */

import { useState } from "react";
import { RotateCcw, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { cn, formatDateTime, pluralize } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { useFlowVersions } from "@/hooks/api/useFlowApi";
import { formatTimeAgo } from "@/lib/relative-time";

interface VersionHistoryPanelProps {
  flowId: string;
  /** True while a restore is in flight. */
  isRestoring: boolean;
  readOnly?: boolean;
  onRestore: (versionNumber: number) => void;
  onClose: () => void;
}

export function VersionHistoryPanel({
  flowId,
  isRestoring,
  readOnly,
  onRestore,
  onClose,
}: VersionHistoryPanelProps) {
  const { t_i18n, intl } = useFormatter();
  const { data, isLoading } = useFlowVersions(flowId);
  const [confirming, setConfirming] = useState<number | null>(null);

  return (
    <aside className="flex w-72 shrink-0 flex-col border-l border-elevation-subtle layer-1 bg-elevation-default">
      <div className="flex items-center gap-2 border-b border-elevation-subtle p-3">
        <p className="flex-1 content-base font-medium">{t_i18n("Version history")}</p>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label={t_i18n("Close")}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {isLoading ? (
          <p className="px-2 py-6 text-center content-compact text-default-secondary">
            {t_i18n("Loading…")}
          </p>
        ) : !data || data.items.length === 0 ? (
          <p className="px-2 py-6 text-center content-compact text-default-secondary">
            {t_i18n("No versions yet")}
          </p>
        ) : (
          data.items.map((version) => (
            <div
              key={version.id}
              className={cn(
                "mb-1 rounded-sm border p-2.5",
                version.is_active ? "border-accent/40 bg-accent/[0.04]" : "border-elevation-subtle",
              )}
            >
              <div className="flex items-center gap-2">
                <span className="content-compact font-medium">v{version.version_number}</span>
                {version.is_active && (
                  <Badge variant="outline" className="text-[9px] text-accent">
                    {t_i18n("Current")}
                  </Badge>
                )}
                <span
                  className="ml-auto content-caption text-default-secondary"
                  title={formatDateTime(version.created_at)}
                >
                  {formatTimeAgo(intl, version.created_at)}
                </span>
              </div>

              <p className="mt-1 content-caption text-default-secondary">
                {version.commit_message || t_i18n("No message")}
              </p>
              <p className="content-caption text-default-secondary">
                {version.author_name ?? t_i18n("Unknown author")} ·{" "}
                {pluralize(version.node_count, "node")}
              </p>

              {!version.is_active && !readOnly && (
                confirming === version.version_number ? (
                  <div className="mt-2 flex items-center gap-1.5">
                    <Button
                      size="sm"
                      className="h-6 flex-1 content-caption"
                      disabled={isRestoring}
                      onClick={() => {
                        onRestore(version.version_number);
                        setConfirming(null);
                      }}
                    >
                      {t_i18n("Restore")}
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-6 content-caption"
                      onClick={() => setConfirming(null)}
                    >
                      {t_i18n("Cancel")}
                    </Button>
                  </div>
                ) : (
                  <Button
                    size="sm"
                    variant="outline"
                    className="mt-2 h-6 w-full gap-1 content-caption"
                    onClick={() => setConfirming(version.version_number)}
                  >
                    <RotateCcw className="h-3 w-3" /> {t_i18n("Restore this version")}
                  </Button>
                )
              )}
            </div>
          ))
        )}
      </div>

      {data && (
        <p className="border-t border-elevation-subtle px-3 py-2 content-caption text-default-secondary">
          {t_i18n("The last {count} versions are kept.", { count: data.max_entries })}
        </p>
      )}
    </aside>
  );
}
