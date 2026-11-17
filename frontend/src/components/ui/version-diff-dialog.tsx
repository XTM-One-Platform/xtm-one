// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import DiffViewer from "react-diff-viewer-continued";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { useFormatter } from "@/i18n";
import type { VersionDiffResponse } from "@/types";

interface VersionDiffDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  diff: VersionDiffResponse | null;
  title: string;
  /** What changed, in words, under the title. */
  description?: string | null;
}

export function VersionDiffDialog({
  open,
  onOpenChange,
  diff,
  title,
  description,
}: VersionDiffDialogProps) {
  const { t_i18n } = useFormatter();
  const [view, setView] = useState<"split" | "unified">("split");
  const oldValue = diff?.lines
    .filter((line) => line.type !== "add")
    .map((line) => line.content)
    .join("\n") ?? "";
  const newValue = diff?.lines
    .filter((line) => line.type !== "remove")
    .map((line) => line.content)
    .join("\n") ?? "";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-6xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
          {diff && (
            <div className="mt-2 flex items-center gap-2">
              <Badge variant="outline">v{diff.from_version} {"->"} v{diff.to_version}</Badge>
              <Badge variant="outline" className="text-feedback-success-primary border-feedback-success-primary/40">+{diff.added}</Badge>
              <Badge variant="outline" className="text-feedback-error-primary border-feedback-error-primary/40">-{diff.removed}</Badge>
              <SegmentedFilter
                ariaLabel={t_i18n("Diff view")}
                className="ml-auto"
                value={view}
                onChange={setView}
                options={[
                  { key: "split", label: t_i18n("Side by side") },
                  { key: "unified", label: t_i18n("Unified") },
                ]}
              />
            </div>
          )}
        </DialogHeader>

        {diff ? (
          <div className="space-y-3">
            <div className="max-h-[60vh] overflow-auto rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2">
              <DiffViewer
                oldValue={oldValue}
                newValue={newValue}
                splitView={view === "split"}
                hideLineNumbers={false}
                useDarkTheme
                showDiffOnly={false}
              />
            </div>
            <details className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
              <DisclosureSummary>{t_i18n("Unified diff (raw)")}</DisclosureSummary>
              <pre className="mt-2 whitespace-pre-wrap content-compact text-accent max-h-56 overflow-auto">{diff.unified_diff || t_i18n("No textual diff.")}</pre>
            </details>
          </div>
        ) : (
          <p className="content-base text-default-secondary">{t_i18n("No diff data available.")}</p>
        )}
      </DialogContent>
    </Dialog>
  );
}
