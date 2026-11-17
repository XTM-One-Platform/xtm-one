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
 * Modal script authoring for the Script node.
 *
 * The right drawer is ~268px of usable width — writing real code there was
 * the worst squeeze in the builder. This dialog gives the creation flow the
 * room it needs over the canvas: a two-column form, a highlighted code
 * editor (CodeEditor) and a Validate button wired to POST
 * /custom-tools/lint — a server-side SYNTAX check that never executes the
 * script (Python ast.parse; node --check on the runtime-wrapped source).
 * Create runs the same lint first: a script that cannot parse never lands
 * in the platform. The created object is a plain Custom tool — the full
 * editor in Tools keeps ownership of later edits.
 */

import { useState } from "react";
import { CheckCircle2, Plus, X, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  useCreateCustomTool,
  useLintCustomTool,
} from "@/hooks/api/useCustomToolApi";
import { useFormatter } from "@/i18n";
import type { CustomTool, CustomToolLanguage, JsonSchema } from "@/types";
import {
  buildSchema,
  newSchemaRow,
  PARAM_TYPE_OPTIONS,
  type SchemaPropertyRow,
} from "@/pages/custom-tool-detail/schema-utils";
import { CustomToolCodeGenerator } from "@/pages/custom-tool-detail/CustomToolCodeGenerator";
import { CodeEditor } from "./CodeEditor";

interface ScriptCreatorDialogProps {
  onClose: () => void;
  onCreated: (created: { id: string; name: string }) => void;
}

type LintState =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "ok" }
  | { kind: "error"; message: string; line?: number | null };

export function ScriptCreatorDialog({
  onClose,
  onCreated,
}: ScriptCreatorDialogProps) {
  const { t_i18n } = useFormatter();
  const createTool = useCreateCustomTool();
  const lintTool = useLintCustomTool();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [language, setLanguage] = useState<CustomToolLanguage>("python");
  const [code, setCode] = useState("");
  const [rows, setRows] = useState<SchemaPropertyRow[]>([]);
  const [timeout, setTimeoutSeconds] = useState(60);
  const [lint, setLint] = useState<LintState>({ kind: "idle" });
  const [error, setError] = useState<string | null>(null);

  const setRow = (rowId: string, patch: Partial<SchemaPropertyRow>) =>
    setRows((rs) => rs.map((r) => (r.rowId === rowId ? { ...r, ...patch } : r)));

  const runLint = async (): Promise<boolean> => {
    setLint({ kind: "checking" });
    try {
      const verdict = (await lintTool.mutateAsync({ language, code })) as {
        status: string;
        message?: string;
        line?: number | null;
      };
      if (verdict.status === "ok") {
        setLint({ kind: "ok" });
        return true;
      }
      setLint({
        kind: "error",
        message: verdict.message || t_i18n("Invalid syntax."),
        line: verdict.line,
      });
      return false;
    } catch (e) {
      setLint({
        kind: "error",
        message: e instanceof Error ? e.message : String(e),
      });
      return false;
    }
  };

  const submit = async () => {
    setError(null);
    // A script that cannot parse never lands in the platform.
    if (!(await runLint())) return;
    try {
      const created = (await createTool.mutateAsync({
        name: name.trim(),
        description: description.trim(),
        language,
        js_function: code,
        input_schema: buildSchema(rows) as JsonSchema,
        timeout,
      })) as CustomTool;
      onCreated({ id: created.id, name: created.name });
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{t_i18n("Write a new script")}</DialogTitle>
          <DialogDescription>
            {t_i18n(
              "The script becomes a platform Custom tool: reusable from agents, chat and other flows, editable later in Tools.",
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1">
            <Label htmlFor="script-creator-name">
              {t_i18n("Tool name")} <span className="text-feedback-error-primary">*</span>
            </Label>
            <Input aria-label={t_i18n("Tool name")}
              id="script-creator-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="script-creator-description">{t_i18n("Description")}</Label>
            <Input aria-label={t_i18n("Description")}
              id="script-creator-description"
              placeholder={t_i18n("What it does (shown to agents too)")}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
        </div>

        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1">
            {(["python", "javascript"] as const).map((lang) => (
              <Button
                key={lang}
                variant={language === lang ? "secondary" : "ghost"}
                aria-pressed={language === lang}
                onClick={() => {
                  setLanguage(lang);
                  setLint({ kind: "idle" });
                }}
              >
                {lang === "python" ? "Python" : "JavaScript"}
              </Button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <Label htmlFor="script-creator-timeout" className="text-default-secondary">
              {t_i18n("Timeout (seconds)")}
            </Label>
            <Input aria-label={t_i18n("Timeout (seconds)")}
              id="script-creator-timeout"
              type="number"
              className="w-20"
              min={1}
              max={300}
              value={timeout}
              onChange={(e) =>
                setTimeoutSeconds(
                  Math.max(1, Math.min(300, Number(e.target.value) || 60)),
                )
              }
            />
          </div>
        </div>

        <div className="space-y-1.5">
          <div className="flex items-center justify-between">
            <Label>{t_i18n("Declared inputs")}</Label>
            <Button
              variant="ghost"
              className="gap-1"
              onClick={() => setRows((rs) => [...rs, newSchemaRow()])}
            >
              <Plus className="h-3 w-3" />
              {t_i18n("Add an input")}
            </Button>
          </div>
          {rows.length === 0 && (
            <p className="content-compact text-default-secondary">
              {t_i18n(
                "Inputs reach the script as variables: $name in JavaScript, name in Python.",
              )}
            </p>
          )}
          {rows.map((row) => (
            <div key={row.rowId} className="flex items-center gap-2">
              <Input aria-label={t_i18n("input name")}
                className="flex-1 font-mono-plex"
                placeholder={t_i18n("input name")}
                value={row.name}
                onChange={(e) => setRow(row.rowId, { name: e.target.value })}
              />
              <Input aria-label={t_i18n("Description")}
                className="flex-1"
                placeholder={t_i18n("Description")}
                value={row.description}
                onChange={(e) =>
                  setRow(row.rowId, { description: e.target.value })
                }
              />
              <Select
                value={row.type}
                onValueChange={(v) =>
                  setRow(row.rowId, { type: v as SchemaPropertyRow["type"] })
                }
              >
                <SelectTrigger className="w-28" aria-label={t_i18n("Type")}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {PARAM_TYPE_OPTIONS.map((tp) => (
                    <SelectItem key={tp} value={tp}>
                      {tp}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <div className="flex items-center gap-1">
                <Switch
                  checked={row.required}
                  onCheckedChange={(checked) =>
                    setRow(row.rowId, { required: checked })
                  }
                  aria-label={t_i18n("Required")}
                />
                <Button
                  variant="ghost"
                  size="icon"
                  className="shrink-0"
                  onClick={() =>
                    setRows((rs) => rs.filter((r) => r.rowId !== row.rowId))
                  }
                  aria-label={t_i18n("Remove input")}
                >
                  <X className="h-3 w-3" />
                </Button>
              </div>
            </div>
          ))}
        </div>

        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <Label>
              {t_i18n("Script")} <span className="text-feedback-error-primary">*</span>
            </Label>
            <CustomToolCodeGenerator
              language={language}
              code={code}
              onGenerated={(result) => {
                setCode(result.code);
                setLint(
                  result.syntax_ok
                    ? { kind: "ok" }
                    : {
                        kind: "error",
                        message: result.syntax_error || t_i18n("Invalid syntax."),
                        line: result.syntax_error_line,
                      },
                );
              }}
              toolName={name}
              toolDescription={description}
              parameters={rows}
              showsSyntax
              className="h-9"
            />
          </div>
          <CodeEditor
            value={code}
            language={language}
            onChange={(v) => {
              setCode(v);
              if (lint.kind !== "idle") setLint({ kind: "idle" });
            }}
            className="max-h-72 min-h-56"
            placeholder={
              language === "python"
                ? 'import json\nprint(json.dumps({"result": name}))'
                : "console.log(JSON.stringify({ result: $name }));"
            }
          />
          {lint.kind === "ok" && (
            <p className="flex items-center gap-1.5 content-compact text-feedback-success-primary">
              <CheckCircle2 className="h-3.5 w-3.5" />
              {t_i18n("Syntax is valid.")}
            </p>
          )}
          {lint.kind === "error" && (
            <div className="flex items-start gap-1.5 content-compact text-feedback-error-primary">
              <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <pre className="whitespace-pre-wrap font-mono-plex text-[11px]">
                {lint.line ? `${t_i18n("Line")} ${lint.line}: ` : ""}
                {lint.message}
              </pre>
            </div>
          )}
          {error && <p className="content-compact text-feedback-error-primary">{error}</p>}
        </div>

        <div className="flex items-center justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            variant="outline"
            disabled={!code.trim() || lint.kind === "checking"}
            onClick={runLint}
          >
            {lint.kind === "checking"
              ? t_i18n("Checking…")
              : t_i18n("Validate")}
          </Button>
          <Button
            disabled={!name.trim() || !code.trim() || createTool.isPending}
            onClick={submit}
          >
            {createTool.isPending
              ? t_i18n("Creating…")
              : t_i18n("Create and bind")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
