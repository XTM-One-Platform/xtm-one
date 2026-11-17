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
 * Import MCP servers from a configuration document.
 *
 * Two steps in one dialog rather than a chain of them. First a choice — a
 * file from disk, or a URL — because the same document lives in both places
 * and the user knows which they have. Then a review of what was found, which
 * is what stops an import from being a leap of faith: a config file can hold
 * a dozen servers, and dropping all of them into someone's list unasked is
 * not an import, it is a surprise.
 *
 * The parsing happens server-side (see `app.services.mcp_config_import`), so
 * this component never learns the difference between a client's `mcpServers`
 * block and a vendor's `server.json`.
 */

import { useMemo, useRef, useState } from "react";
import {
  AlertTriangle, ArrowLeft, CheckCircle2, FileJson, Globe, Terminal, Upload, XCircle,
} from "lucide-react";
import { Spinner } from "@filigran/design-system";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toast";
import { usePreviewMCPConfig, useImportMCPConfig } from "@/hooks/api/useMCPServerApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { MCPConfigCandidate, MCPConfigPreviewResponse } from "@/types";

/** Matches the backend's own ceiling, so an oversized file is refused here
 *  rather than after a pointless round trip. */
const MAX_FILE_BYTES = 1_000_000;

type Step = "choose" | "url" | "review";

export function MCPImportDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const preview = usePreviewMCPConfig();
  const importMutation = useImportMCPConfig();
  // Both endpoints sit behind the agentic gate, so every control that reaches
  // them is disabled rather than left to fail on click (FR-018).
  const aiOff = useAiActionDisabled();
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const [step, setStep] = useState<Step>("choose");
  const [url, setUrl] = useState("");
  const [sourceLabel, setSourceLabel] = useState("");
  const [found, setFound] = useState<MCPConfigPreviewResponse | null>(null);
  const [deselected, setDeselected] = useState<Set<string>>(new Set());
  const [dragging, setDragging] = useState(false);

  const reset = () => {
    setStep("choose");
    setUrl("");
    setSourceLabel("");
    setFound(null);
    setDeselected(new Set());
    setDragging(false);
    preview.reset();
    importMutation.reset();
  };

  const close = (next: boolean) => {
    if (!next) reset();
    onOpenChange(next);
  };

  const runPreview = (body: { content?: string; url?: string }, label: string) => {
    preview.mutate(body, {
      onSuccess: (data) => {
        setFound(data);
        setSourceLabel(label);
        setDeselected(new Set());
        setStep("review");
      },
      onError: (err) =>
        toast({
          title: t_i18n("Could not read that configuration"),
          description: err.message,
          variant: "error",
        }),
    });
  };

  const handleFile = (file: File) => {
    if (file.size > MAX_FILE_BYTES) {
      toast({
        title: t_i18n("File too large"),
        description: t_i18n("An MCP configuration should be well under 1 MB."),
        variant: "error",
      });
      return;
    }
    file
      .text()
      .then((content) => runPreview({ content }, file.name))
      .catch(() =>
        toast({ title: t_i18n("Could not read that file"), variant: "error" }),
      );
  };

  const selected = useMemo(
    () => (found?.candidates ?? []).filter((c) => !deselected.has(c.source || c.name)),
    [found, deselected],
  );

  const toggle = (candidate: MCPConfigCandidate) => {
    const key = candidate.source || candidate.name;
    setDeselected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const confirm = () => {
    importMutation.mutate(
      { servers: selected },
      {
        onSuccess: (result) => {
          if (result.created.length > 0) {
            toast({
              title: t_i18n("Import complete"),
              description: t_i18n("{count, plural, one {# server} other {# servers}} added.", { count: result.created.length }),
              variant: "success",
            });
          }
          // A partial failure keeps the dialog open, because closing would
          // hide which entries did not land. But what stays on screen is now
          // *only* those entries: leaving the successful ones selected meant
          // a second click re-sent them and created duplicates, since the
          // import has no idea it already ran.
          if (result.failed.length > 0) {
            const reasons = new Map(result.failed.map((f) => [f.name, f.error]));
            setFound({
              format: found?.format ?? "",
              candidates: (found?.candidates ?? []).filter((c) => reasons.has(c.name)),
              warnings: result.failed.map((f) => `${f.name}: ${f.error}`),
            });
            setDeselected(new Set());
            toast({
              title: t_i18n("Some servers were not imported"),
              description: result.failed.map((f) => `${f.name}: ${f.error}`).join("\n"),
              variant: "error",
            });
            return;
          }
          close(false);
        },
        onError: (err) =>
          toast({ title: t_i18n("Import failed"), description: err.message, variant: "error" }),
      },
    );
  };

  const busy = preview.isPending || importMutation.isPending || aiOff;

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t_i18n("Import MCP servers")}</DialogTitle>
          <DialogDescription>
            {step === "review"
              ? t_i18n("Choose what to add from {source}.", { source: sourceLabel })
              : t_i18n(
                  "Bring in servers from a client configuration (mcpServers) or a server.json.",
                )}
          </DialogDescription>
        </DialogHeader>

        {/* ---- Step 1: where the document is ---- */}
        {step === "choose" && (
          <div className="grid gap-4 sm:grid-cols-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => fileInputRef.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragging(false);
                const file = e.dataTransfer.files?.[0];
                if (file) handleFile(file);
              }}
              className={cn(
                "flex flex-col items-center gap-3 rounded-sm border border-dashed p-6 text-center transition-all",
                "hover:bg-elevation-hover disabled:opacity-60",
                dragging && "border-accent bg-accent/5",
              )}
            >
              <div className="flex h-10 w-10 items-center justify-center rounded-sm bg-accent/10">
                {preview.isPending && <Spinner size="md" tone="inherit" className="text-accent" />}
                {!preview.isPending && <Upload className="h-5 w-5 text-accent" />}
              </div>
              <div>
                <p className="content-base font-semibold">{t_i18n("From a file")}</p>
                <p className="mt-1 content-compact text-default-secondary">
                  {t_i18n("Pick a .json file, or drop one here.")}
                </p>
              </div>
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept="application/json,.json"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) handleFile(file);
                // Cleared so picking the same file twice fires onChange again.
                e.target.value = "";
              }}
            />

            <button
              type="button"
              disabled={busy}
              onClick={() => setStep("url")}
              className="flex flex-col items-center gap-3 rounded-sm border border-dashed p-6 text-center transition-all hover:bg-elevation-hover disabled:opacity-60"
            >
              <div className="flex h-10 w-10 items-center justify-center rounded-sm bg-accent/10">
                <Globe className="h-5 w-5 text-accent" />
              </div>
              <div>
                <p className="content-base font-semibold">{t_i18n("From a URL")}</p>
                <p className="mt-1 content-compact text-default-secondary">
                  {t_i18n("Fetch a published server.json or config.")}
                </p>
              </div>
            </button>
          </div>
        )}

        {/* ---- Step 1b: the URL ---- */}
        {step === "url" && (
          <div className="space-y-4">
            <div className="space-y-2">
              <label className="content-base font-medium" htmlFor="mcp-import-url">
                {t_i18n("Configuration URL")}
              </label>
              <Input aria-label={t_i18n("Configuration URL")}
                id="mcp-import-url"
                autoFocus
                placeholder="https://example.com/server.json"
                value={url}
                disabled={busy}
                onChange={(e) => setUrl(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && url.trim() && !busy) runPreview({ url: url.trim() }, url.trim());
                }}
              />
              <p className="content-compact text-default-secondary">
                {t_i18n(
                  "The document is fetched by the platform, so it must be reachable from the server and publicly addressable.",
                )}
              </p>
            </div>
            <div className="flex justify-between">
              <Button variant="ghost" onClick={() => setStep("choose")} disabled={busy}>
                <ArrowLeft className="mr-1.5 h-4 w-4" /> {t_i18n("Back")}
              </Button>
              <AiDisabledTip>
                <Button
                  onClick={() => runPreview({ url: url.trim() }, url.trim())}
                  className="gap-1.5"
                  disabled={!url.trim() || busy}
                  aria-busy={preview.isPending || undefined}
                >
                  {preview.isPending && <Spinner size="sm" tone="inherit" />}
                  {t_i18n("Fetch")}
                </Button>
              </AiDisabledTip>
            </div>
          </div>
        )}

        {/* ---- Step 2: what was found ---- */}
        {step === "review" && found && (
          <div className="space-y-4">
            {found.candidates.length > 0 && (
              <div className="max-h-[320px] space-y-2 overflow-y-auto pr-1">
                {found.candidates.map((c) => {
                  const key = c.source || c.name;
                  const on = !deselected.has(key);
                  return (
                    <button
                      type="button"
                      key={key}
                      onClick={() => toggle(c)}
                      // A toggle, not a command: without this the only cue
                      // that a row is selected is its icon, colour and
                      // opacity, none of which a screen reader announces.
                      aria-pressed={on}
                      className={cn(
                        "flex w-full items-start gap-3 rounded-sm border p-3 text-left transition-all",
                        on ? "border-accent/40 bg-accent/[0.04]" : "opacity-55",
                      )}
                    >
                      <div className="mt-0.5 shrink-0">
                        {on ? (
                          <CheckCircle2 className="h-4 w-4 text-accent" />
                        ) : (
                          <XCircle className="h-4 w-4 text-default-secondary" />
                        )}
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <p className="truncate content-base font-semibold">{c.name}</p>
                          <Badge variant="outline" className="gap-1 content-caption">
                            {c.transport === "stdio" ? (
                              <Terminal className="h-3 w-3" />
                            ) : (
                              <Globe className="h-3 w-3" />
                            )}
                            {c.transport === "stdio" ? "stdio" : "HTTP"}
                          </Badge>
                        </div>
                        <p className="mt-1 truncate font-mono-plex content-compact text-default-secondary">
                          {c.transport === "stdio"
                            ? [c.command, ...c.args].join(" ")
                            : c.url}
                        </p>
                        {/* Said up front, because a server that imports cleanly
                            and then cannot connect is the worst outcome here. */}
                        {c.missing_secrets.length > 0 && (
                          <p className="mt-1.5 flex items-start gap-1.5 content-compact text-feedback-warning-primary">
                            <AlertTriangle className="mt-px h-3 w-3 shrink-0" />
                            {t_i18n("Still needs a value for {keys}", {
                              keys: c.missing_secrets.join(", "),
                            })}
                          </p>
                        )}
                      </div>
                    </button>
                  );
                })}
              </div>
            )}

            {found.warnings.length > 0 && (
              <div className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.06] p-3">
                <p className="flex items-center gap-1.5 content-compact font-medium text-feedback-warning-primary">
                  <AlertTriangle className="h-3.5 w-3.5" />
                  {t_i18n("Not imported")}
                </p>
                <ul className="mt-1.5 space-y-1">
                  {found.warnings.map((w) => (
                    <li key={w} className="content-compact text-default-secondary">
                      {w}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {found.candidates.length === 0 && (
              <div className="flex flex-col items-center gap-2 py-6 text-center">
                <FileJson className="h-8 w-8 text-default-secondary" />
                <p className="content-base text-default-secondary">
                  {t_i18n("Nothing in that document could be imported.")}
                </p>
              </div>
            )}

            <div className="flex items-center justify-between">
              <Button variant="ghost" onClick={() => setStep("choose")} disabled={busy}>
                <ArrowLeft className="mr-1.5 h-4 w-4" /> {t_i18n("Choose another")}
              </Button>
              <div className="flex items-center gap-3">
                <span className="content-compact text-default-secondary">
                  {t_i18n("{count} selected", { count: selected.length })}
                </span>
                <AiDisabledTip>
                  <Button
                    className="gap-1.5"
                    onClick={confirm}
                    disabled={selected.length === 0 || busy}
                    aria-busy={importMutation.isPending || undefined}
                  >
                    {importMutation.isPending && <Spinner size="sm" tone="inherit" />}
                    {t_i18n("Import")}
                  </Button>
                </AiDisabledTip>
              </div>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
