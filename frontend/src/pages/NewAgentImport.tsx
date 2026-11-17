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

import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  AlertTriangle, CheckCircle2, Circle, FileUp, Info, Minus, Sparkles, Upload, XCircle,
} from "lucide-react";
import { Spinner } from "@filigran/design-system";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { BackButton } from "@/components/ui/back-button";
import { useToast } from "@/components/ui/toast";
import {
  useCheckAgentImportFile, useConfirmAgentImport, useSuggestImportFixes,
} from "@/hooks/api/useAgentApi";
import { AgenticGatePage } from "@/components/ui/license-gate-dialog";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { AgentImportCheckStage, AgentImportFileCheckResult } from "@/types";

/** Stage labels only — the server's response decides which stages exist and
 *  in what order, so a stage added backend-side renders without a UI change.
 *  Keyed on the API union: a missing label is a compile error. Read by
 *  `t_i18n(STAGE_LABELS[stage.key])`, so every value is a catalog key
 *  `validate-i18n` checks. @i18n-keys */
const STAGE_LABELS = {
  parse: "Reading the file",
  detect: "Detecting the format",
  schema: "Validating against the schema",
  references: "Resolving references",
} as const satisfies Record<AgentImportCheckStage["key"], string>;

/** Placeholder rows shown while the upload + check request is in flight. */
const PENDING_STAGES = (Object.keys(STAGE_LABELS) as AgentImportCheckStage["key"][]).map(
  (key): AgentImportCheckStage => ({ key, status: "skipped", problems: [] }),
);

/** Delay between two stage reveals — presentation pacing only, the verdicts
 *  all arrived with the check response. */
const REVEAL_INTERVAL_MS = 150;

/**
 * Import page at /agents/new/import — upload an agent file, watch the
 * verification loop run stage by stage, review what the platform could not
 * satisfy, and only then confirm the import.
 *
 * The check is a server-side dry-run (POST /agents/import-file/check): nothing
 * is persisted until the user confirms, and the confirm call imports the exact
 * bundle the check returned rather than re-uploading the file blind.
 */
export default function NewAgentImport() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const agenticAIEnabled = useAgenticAIEnabled();
  const aiOff = useAiActionDisabled();
  const check = useCheckAgentImportFile();
  const confirm = useConfirmAgentImport();
  const suggest = useSuggestImportFixes();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  // How many stage verdicts are shown so far — the check answers in one
  // response, revealed row by row so the user can follow the loop.
  const [revealed, setRevealed] = useState(0);
  // Correction ids (from the check report) the user chose to apply.
  const [selectedCorrections, setSelectedCorrections] = useState<Set<string>>(new Set());

  const result: AgentImportFileCheckResult | undefined = check.data;
  const fullyRevealed = !!result && revealed >= result.stages.length;

  useEffect(() => {
    if (!result) return;
    if (revealed >= result.stages.length) return;
    // A failing document skips the pacing entirely — the user wants the
    // error now, not a replay.
    if (!result.ok) {
      setRevealed(result.stages.length);
      return;
    }
    const timer = setTimeout(() => setRevealed((n) => n + 1), REVEAL_INTERVAL_MS);
    return () => clearTimeout(timer);
  }, [result, revealed]);

  if (!agenticAIEnabled || aiOff) return <AgenticGatePage feature="Creating agents" />;

  const startCheck = (file: File | undefined) => {
    if (!file) return;
    setFileName(file.name);
    setRevealed(0);
    setSelectedCorrections(new Set());
    suggest.reset();
    check.mutate(file, {
      onError: (err) => {
        toast({ title: t_i18n("Import failed"), description: err.message, variant: "error" });
        setFileName(null);
      },
    });
  };

  const reset = () => {
    setFileName(null);
    setRevealed(0);
    setSelectedCorrections(new Set());
    check.reset();
    suggest.reset();
  };

  const toggleCorrection = (id: string, on: boolean) => {
    setSelectedCorrections((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const handleSuggest = () => {
    if (!result) return;
    const agentSection = result.bundle?.agent as Record<string, unknown> | undefined;
    suggest.mutate(
      {
        agent_name: result.agent_name ?? undefined,
        persona: typeof agentSection?.persona === "string" ? agentSection.persona : undefined,
        warnings: result.warnings,
      },
      {
        onError: (err) => {
          toast({ title: t_i18n("Suggestions unavailable"), description: err.message, variant: "error" });
        },
      },
    );
  };

  const handleConfirm = () => {
    if (!result?.bundle || !result.detected_format) return;
    confirm.mutate(
      {
        bundle: result.bundle,
        detected_format: result.detected_format,
        corrections: [...selectedCorrections],
      },
      {
        onSuccess: (imported) => {
          toast({
            title: t_i18n("Agent imported"),
            description: [
              t_i18n("\"{name}\" was imported from a {format} file.", {
                name: imported.agent.name,
                format: imported.detected_format_label,
              }),
              ...imported.notes,
              ...imported.warnings,
            ].join(" "),
            variant: imported.warnings.length > 0 ? "info" : "success",
          });
          navigate(`/agents/${imported.agent.id}`);
        },
        onError: (err) => {
          toast({ title: t_i18n("Import failed"), description: err.message, variant: "error" });
        },
      },
    );
  };

  return (
    <div className="space-y-6">
      <BackButton to="/agents/new" label="Back to Creation Modes" />
      <div>
        <h1 className="text-2xl font-bold">{t_i18n("Import an agent")}</h1>
        <p className="mt-2 text-default-secondary">{t_i18n("Upload an agent file, review what this platform can satisfy, then confirm the import.")}</p>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        accept=".json,.yaml,.yml"
        className="hidden"
        onChange={(e) => { startCheck(e.target.files?.[0]); e.target.value = ""; }}
      />

      {!fileName ? (
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => { e.preventDefault(); setDragging(false); startCheck(e.dataTransfer.files?.[0]); }}
          className={cn(
            "flex w-full max-w-3xl flex-col items-center gap-3 rounded-sm border-2 border-dashed p-12 text-center transition-colors",
            dragging ? "border-accent bg-accent/5" : "border-elevation-subtle-soft hover:border-elevation-subtle hover:bg-elevation-hover",
          )}
        >
          <div className="flex h-12 w-12 items-center justify-center rounded-sm bg-accent/10">
            <Upload className="h-6 w-6 text-accent" />
          </div>
          <p className="content-base font-medium">{t_i18n("Drop an agent file here, or click to browse")}</p>
          <p className="content-compact text-default-secondary">{t_i18n("XTM One native bundle or Microsoft AgentSchema document (JSON or YAML)")}</p>
        </button>
      ) : (
        <div className="max-w-3xl space-y-4">
          <Card>
            <CardContent className="pt-6 space-y-4">
              <div className="flex items-center gap-3">
                <FileUp className="h-4 w-4 text-default-secondary shrink-0" />
                <span className="content-base font-medium truncate">{fileName}</span>
                {result?.detected_format_label && fullyRevealed && (
                  <Badge variant="outline" className="text-[9px] text-accent border-accent/30 shrink-0">{result.detected_format_label}</Badge>
                )}
                <Button variant="ghost" size="sm" className="ml-auto shrink-0 text-default-secondary" onClick={reset}>
                  {t_i18n("Choose another file")}
                </Button>
              </div>

              {/* The verification loop, stage by stage. Rows come from the
                  RESPONSE (a stage added server-side renders with no UI
                  change); while the upload + check request is in flight the
                  known stages render as placeholders with the first one
                  spinning, so a slow upload is distinguishable from the
                  result playback. */}
              <div className="space-y-2">
                {(result?.stages ?? PENDING_STAGES).map((stage, i) => {
                  const shown = !!result && i < revealed;
                  const current = result ? i === revealed : i === 0;
                  return (
                    <div key={stage.key} className="flex items-start gap-2.5 content-base">
                      {shown ? (
                        stage.status === "passed" ? (
                          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-feedback-success-primary" />
                        ) : stage.status === "failed" ? (
                          <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-feedback-error-primary" />
                        ) : (
                          <Minus className="mt-0.5 h-4 w-4 shrink-0 text-default-secondary" />
                        )
                      ) : current ? (
                        <Spinner size="sm" tone="inherit" className="mt-0.5 shrink-0 text-default-secondary" label={t_i18n("In progress")} />
                      ) : (
                        <Circle className="mt-0.5 h-4 w-4 shrink-0 text-default-disabled" />
                      )}
                      <div className="min-w-0 flex-1">
                        <span className={cn(!shown ? "text-default-secondary" : stage.status === "skipped" ? "text-default-secondary" : "")}>
                          {t_i18n(STAGE_LABELS[stage.key])}
                        </span>
                        {shown && stage.status === "failed" && (
                          <ul className="mt-1 space-y-1">
                            {stage.problems.map((p, j) => (
                              <li key={j} className="content-compact text-feedback-error-primary">{p}</li>
                            ))}
                          </ul>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* Verdict + report, once the loop has fully played out */}
              {fullyRevealed && result && (
                <div className="space-y-3 border-t border-elevation-subtle-soft pt-4">
                  {result.ok ? (
                    <div className="flex items-center gap-2 content-base font-medium text-feedback-success-primary">
                      <CheckCircle2 className="h-4 w-4" />
                      {result.agent_name
                        ? t_i18n("\"{name}\" is ready to import.", { name: result.agent_name })
                        : t_i18n("This file is ready to import.")}
                    </div>
                  ) : (
                    <div className="flex items-center gap-2 content-base font-medium text-feedback-error-primary">
                      <XCircle className="h-4 w-4" />
                      {t_i18n("This file cannot be imported.")}
                    </div>
                  )}

                  {result.notes.length > 0 && (
                    <div className="space-y-1">
                      {result.notes.map((note, i) => (
                        <div key={i} className="flex items-start gap-2 content-compact text-default-secondary">
                          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                          <span>{note}</span>
                        </div>
                      ))}
                    </div>
                  )}

                  {result.warnings.length > 0 && (
                    <div className="space-y-1 rounded-sm bg-feedback-alert-primary/5 p-3">
                      <p className="content-compact font-medium text-feedback-warning-primary">{t_i18n("This platform cannot satisfy everything the file references:")}</p>
                      {result.warnings.map((warning, i) => (
                        <div key={i} className="flex items-start gap-2 content-compact text-feedback-warning-primary">
                          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                          <span>{warning}</span>
                        </div>
                      ))}
                      <p className="pt-1 text-[11px] text-default-secondary">{t_i18n("The agent will be imported without these; you can finish the setup afterwards.")}</p>
                      <div className="pt-1">
                        {suggest.data ? (
                          <div className="space-y-1 rounded-sm bg-elevation-hover p-3">
                            <p className="flex items-center gap-1.5 content-compact font-medium text-accent">
                              <Sparkles className="h-3.5 w-3.5" /> {t_i18n("AI suggestions")}
                            </p>
                            <p className="whitespace-pre-wrap content-compact text-default-secondary">{suggest.data.suggestions}</p>
                          </div>
                        ) : (
                          <Button
                            variant="ghost"
                            size="sm"
                            className="gap-1.5 text-accent hover:text-accent"
                            disabled={suggest.isPending || aiOff}
                            aria-busy={suggest.isPending || undefined}
                            onClick={handleSuggest}
                          >
                            {suggest.isPending ? <Spinner size="sm" tone="inherit" label={t_i18n("Loading...")} /> : <Sparkles className="h-3.5 w-3.5" />}
                            {t_i18n("Suggest fixes with AI")}
                          </Button>
                        )}
                      </div>
                    </div>
                  )}

                  {result.ok && result.corrections.length > 0 && (
                    <div className="space-y-2">
                      <p className="content-compact font-medium">{t_i18n("Corrections you can apply before importing:")}</p>
                      {result.corrections.map((correction) => (
                        <div key={correction.id} className="flex items-center justify-between gap-3 rounded-sm bg-elevation-hover p-3">
                          <span className="content-compact text-default-secondary">
                            {correction.kind === "drop_custom_tool"
                              ? t_i18n("Remove the tool \"{name}\": it was imported without an implementation.", { name: correction.subject })
                              : t_i18n("Remove the MCP server \"{name}\": it was imported as an unconfigured placeholder.", { name: correction.subject })}
                          </span>
                          <Switch
                            aria-label={
                              correction.kind === "drop_custom_tool"
                                ? t_i18n("Remove the tool \"{name}\": it was imported without an implementation.", { name: correction.subject })
                                : t_i18n("Remove the MCP server \"{name}\": it was imported as an unconfigured placeholder.", { name: correction.subject })
                            }
                            checked={selectedCorrections.has(correction.id)}
                            onCheckedChange={(on) => toggleCorrection(correction.id, on)}
                          />
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </CardContent>
          </Card>

          <div className="flex justify-end gap-3">
            <Button variant="ghost" onClick={() => navigate("/agents/new")}>{t_i18n("Cancel")}</Button>
            <Button
              onClick={handleConfirm}
              disabled={!fullyRevealed || !result?.ok || confirm.isPending || aiOff}
            >
              {confirm.isPending ? t_i18n("Importing...") : t_i18n("Import agent")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
