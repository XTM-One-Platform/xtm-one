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

import { useState } from "react";
import { Spinner } from "@filigran/design-system";
import { AlertTriangle, Check, Download, FileJson, FileCode2, Info } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from "@/components/ui/dialog";
import { useAgentExportPortability } from "@/hooks/api/useAgentApi";
import { useFormatter } from "@/i18n";
import type { AgentExportFormat } from "@/types";

// ---------------------------------------------------------------------------
// AgentExportDialog — pick the export format, and see what the chosen format
// cannot carry before the file is generated.
//
// A dialog rather than a dropdown menu because the AgentSchema choice is not a
// like-for-like alternative: it drops or flattens capabilities the native
// bundle keeps, and the user has to see that list to make the choice
// meaningfully. The dialog is only reachable when more than one format is
// available — with AgentSchema disabled, the Export button downloads the
// native bundle directly.
// ---------------------------------------------------------------------------

interface AgentExportDialogProps {
  agentId: string;
  /** Whether an export request is in flight (drives the confirm spinner). */
  isExporting: boolean;
  onExport: (format: AgentExportFormat) => void;
  onClose: () => void;
}

const FORMAT_OPTIONS: {
  format: AgentExportFormat;
  icon: typeof FileJson;
  title: string;
  description: string;
}[] = [
  {
    format: "native",
    icon: FileJson,
    title: "XTM One native bundle",
    description:
      "JSON. Carries everything: assignments, handover, memory settings, prompts, skills and variables. Use this for backups and for moving an agent between XTM One instances.",
  },
  {
    format: "agentschema",
    icon: FileCode2,
    title: "Microsoft AgentSchema",
    description:
      "YAML. An open, cross-vendor agent definition other platforms can read. Only what the specification can express is included as standard fields.",
  },
];

export function AgentExportDialog({
  agentId,
  isExporting,
  onExport,
  onClose,
}: AgentExportDialogProps) {
  const { t_i18n } = useFormatter();
  const [format, setFormat] = useState<AgentExportFormat>("native");

  // Only the AgentSchema format loses anything, so the report is fetched only
  // once that option is actually selected.
  const {
    data: portability,
    isLoading,
    isError,
    refetch,
  } = useAgentExportPortability(agentId, format, format === "agentschema");

  const omitted = portability?.entries.filter((e) => e.disposition === "omitted") ?? [];
  const flattened = portability?.entries.filter((e) => e.disposition === "flattened") ?? [];

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t_i18n("Export agent")}</DialogTitle>
          <DialogDescription>
            {t_i18n("Choose the format to export this agent in.")}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-2">
          {FORMAT_OPTIONS.map((option) => {
            const Icon = option.icon;
            const selected = format === option.format;
            return (
              <button
                key={option.format}
                type="button"
                // The selected state is otherwise conveyed only by the border
                // colour and the check icon, neither of which a screen reader
                // announces.
                aria-pressed={selected}
                onClick={() => setFormat(option.format)}
                className={cn(
                  "flex w-full items-start gap-3 rounded-sm border p-3 text-left transition-colors",
                  selected
                    ? "border-accent bg-accent/5"
                    : "border-elevation-subtle hover:border-elevation-subtle-soft hover:bg-elevation-hover",
                )}
              >
                <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", selected ? "text-accent" : "text-default-secondary")} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 content-base font-medium">
                    {t_i18n(option.title)}
                    {option.format === "native" && (
                      <span className="rounded-sm bg-elevation-hover px-1.5 py-0.5 content-caption font-normal uppercase tracking-wide text-default-secondary">
                        {t_i18n("Default")}
                      </span>
                    )}
                  </div>
                  <p className="mt-1 content-compact text-default-secondary">{t_i18n(option.description)}</p>
                </div>
                {selected && <Check className="mt-0.5 h-4 w-4 shrink-0 text-accent" />}
              </button>
            );
          })}
        </div>

        {format === "agentschema" && (
          <div className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/5 p-3">
            {isLoading ? (
              <div className="flex items-center gap-2 content-compact text-default-secondary">
                <Spinner size="sm" tone="inherit" />
                {t_i18n("Checking what this format can carry…")}
              </div>
            ) : isError || !portability ? (
              // Never fall through to the "nothing will be left out" message on a
              // failed report: the export itself would still work, so silently
              // reassuring the user here would send them off with a file they
              // believe is complete.
              <div className="flex items-start gap-2 content-compact text-feedback-warning-primary">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <div className="min-w-0 flex-1">
                  {t_i18n("Could not check which of this agent's settings AgentSchema can carry. Exporting still works, but some settings may be left out of the standard fields without being listed here.")}
                  <button
                    type="button"
                    onClick={() => void refetch()}
                    className="ml-1 underline underline-offset-2 hover:no-underline"
                  >
                    {t_i18n("Try again")}
                  </button>
                </div>
              </div>
            ) : omitted.length === 0 && flattened.length === 0 ? (
              <div className="flex items-start gap-2 content-compact text-default-secondary">
                <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-feedback-success-primary" />
                {t_i18n("This agent uses no features that AgentSchema cannot express: nothing will be left out.")}
              </div>
            ) : (
              <>
                {omitted.length > 0 && (
                  <>
                    <div className="flex items-center gap-2 content-compact font-medium text-feedback-warning-primary">
                      <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                      {t_i18n("Not included in the AgentSchema fields")}
                    </div>
                    <ul className="mt-2 space-y-1.5">
                      {omitted.map((entry) => (
                        <li key={entry.key} className="content-compact">
                          <span className="font-medium">{entry.label}</span>
                          <span className="text-default-secondary"> — {entry.detail}</span>
                        </li>
                      ))}
                    </ul>
                  </>
                )}
                {flattened.length > 0 && (
                  <>
                    <div className={cn("flex items-center gap-2 content-compact font-medium text-default-secondary", omitted.length > 0 && "mt-3")}>
                      <Info className="h-3.5 w-3.5 shrink-0" />
                      {t_i18n("Included, but no longer as a separate field")}
                    </div>
                    <ul className="mt-2 space-y-1.5">
                      {flattened.map((entry) => (
                        <li key={entry.key} className="content-compact">
                          <span className="font-medium">{entry.label}</span>
                          <span className="text-default-secondary"> — {entry.detail}</span>
                        </li>
                      ))}
                    </ul>
                  </>
                )}
                {portability?.preserved_in_metadata && (
                  <p className="mt-3 content-compact text-default-secondary">
                    {t_i18n("All of the above is still preserved in the document's XTM One metadata, so re-importing the file into XTM One restores it. Other platforms ignore that metadata.")}
                  </p>
                )}
              </>
            )}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            size="sm"
            className="gap-1.5"
            disabled={isExporting || (format === "agentschema" && isLoading)}
            aria-busy={isExporting || undefined}
            onClick={() => onExport(format)}
          >
            {isExporting && <Spinner size="sm" tone="inherit" />}
            {!isExporting && <Download className="h-3.5 w-3.5" aria-hidden="true" />}
            {t_i18n("Export")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
