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

import { Icon } from "@filigran/design-system";
/**
 * A bigger lens on one drawer field.
 *
 * The right drawer is ~268px of usable width — fine for a select, cramped
 * for a prompt, an HTTP body or a script. Every textarea-ish field grows an
 * expand affordance that opens this dialog: the SAME value, edited live
 * through the same onChange (no apply/cancel dance — the drawer behind
 * mirrors every keystroke, and closing is just closing).
 *
 * Three shapes:
 * - variable  — a large VariableInput: the {{ref}} autocomplete survives.
 * - plain     — a large textarea (persona, free text, raw JSON).
 * - code      — the CodeEditor (highlighting) plus a Validate button wired
 *               to the flow lint endpoint (parse-only, never executes).
 */

import type React from "react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useLintFlowCode } from "@/hooks/api/useFlowApi";
import { useFormatter } from "@/i18n";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { cn } from "@/lib/utils";
import { CodeEditor } from "./CodeEditor";
import type { VariableSuggestion } from "./upstream";
import { VariableInput } from "./VariableInput";

interface ExpandedFieldDialogProps {
  title: string;
  shape: "variable" | "plain" | "code";
  value: string;
  suggestions: VariableSuggestion[];
  placeholder?: string;
  disabled?: boolean;
  onChange: (value: string) => void;
  onClose: () => void;
}

type LintState =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "ok" }
  | { kind: "error"; message: string; line?: number | null };

export function ExpandedFieldDialog({
  title,
  shape,
  value,
  suggestions,
  placeholder,
  disabled,
  onChange,
  onClose,
}: ExpandedFieldDialogProps) {
  const { t_i18n } = useFormatter();
  const lintCode = useLintFlowCode();
  const [lint, setLint] = useState<LintState>({ kind: "idle" });

  const runLint = async () => {
    setLint({ kind: "checking" });
    try {
      const verdict = (await lintCode.mutateAsync({ code: value })) as {
        status: string;
        message?: string;
        line?: number | null;
      };
      if (verdict.status === "ok") setLint({ kind: "ok" });
      else
        setLint({
          kind: "error",
          message: verdict.message || t_i18n("Invalid syntax."),
          line: verdict.line,
        });
    } catch (e) {
      setLint({
        kind: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    }
  };

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        {shape === "code" ? (
          <CodeEditor
            value={value}
            language="python"
            onChange={(v) => {
              onChange(v);
              if (lint.kind !== "idle") setLint({ kind: "idle" });
            }}
            className="max-h-[60vh] min-h-72"
            placeholder={placeholder}
          />
        ) : shape === "variable" ? (
          <VariableInput
            aria-label={title}
            value={value}
            onChange={(v) => onChange(String(v))}
            suggestions={suggestions}
            multiline
            rows={16}
            placeholder={placeholder}
            disabled={disabled}
          />
        ) : (
          <textarea
            className={cn("min-h-72 w-full rounded-sm border border-elevation-subtle bg-transparent px-3 py-2 content-base placeholder:text-default-secondary", FIELD_FOCUS_RING)}
            value={value}
            placeholder={placeholder}
            disabled={disabled}
            onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) =>
              onChange(e.target.value)
            }
          />
        )}
        {shape === "code" && lint.kind === "ok" && (
          <p className="flex items-center gap-1.5 content-compact text-feedback-success-primary">
            <Icon name="circle-check" size={16} />
            {t_i18n("Syntax is valid.")}
          </p>
        )}
        {shape === "code" && lint.kind === "error" && (
          <div className="flex items-start gap-1.5 content-compact text-feedback-error-primary">
            <Icon name="circle-x" size={16} className="mt-px shrink-0" />
            <pre className="whitespace-pre-wrap font-mono-plex text-[11px]">
              {lint.line ? `${t_i18n("Line")} ${lint.line}: ` : ""}
              {lint.message}
            </pre>
          </div>
        )}
        <div className="flex items-center justify-end gap-2">
          {shape === "code" && (
            <Button
              variant="outline"
              size="sm"
              disabled={!value.trim() || lint.kind === "checking"}
              onClick={runLint}
            >
              {lint.kind === "checking"
                ? t_i18n("Checking…")
                : t_i18n("Validate")}
            </Button>
          )}
          <Button size="sm" onClick={onClose}>
            {t_i18n("Done")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
