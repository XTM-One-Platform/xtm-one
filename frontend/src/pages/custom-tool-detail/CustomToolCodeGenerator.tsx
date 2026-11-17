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
 * The "Generate with AI" bar of a custom tool's code editor: the
 * shared `PromptGenerator`, fed by `POST /custom-tools/generate-code` with the
 * form as it stands - language, name, description, the declared inputs and
 * the code already written. The code lands in the editor for review; nothing
 * is saved. A request that fails is reported by the global mutation toast.
 * The form stays editable while the model writes, so an answer that arrives
 * after anything it was asked from changed (the code, the language, the
 * inputs, the name or description) is dropped rather than written over those
 * edits; the bar stays open with the brief for another try.
 */
import { useCallback, useRef } from "react";
import { PromptGenerator } from "@/components/ui/prompt-generator";
import { useToast } from "@/components/ui/toast";
import { useGenerateCustomToolCode } from "@/hooks/api/useCustomToolApi";
import { useFormatter } from "@/i18n";
import type { CustomToolLanguage, GenerateCustomToolCodeRequest, GenerateCustomToolCodeResponse } from "@/types";
import type { SchemaProperty } from "./schema-utils";

/** The endpoint's `MAX_PARAMETERS`: a tool declaring more is refused, never cut. */
const MAX_PARAMETERS = 50;

interface CustomToolCodeGeneratorProps {
  language: CustomToolLanguage;
  code: string;
  onGenerated: (result: GenerateCustomToolCodeResponse) => void;
  toolName?: string;
  toolDescription?: string;
  parameters?: SchemaProperty[];
  /** The caller shows the syntax verdict itself, so no toast is raised for it. */
  showsSyntax?: boolean;
  className?: string;
}

export function CustomToolCodeGenerator({
  language,
  code,
  onGenerated,
  toolName,
  toolDescription,
  parameters = [],
  showsSyntax = false,
  className,
}: CustomToolCodeGeneratorProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const generateCode = useGenerateCustomToolCode();
  const lastResult = useRef<GenerateCustomToolCodeResponse | null>(null);
  const form: Omit<GenerateCustomToolCodeRequest, "description"> = {
    language,
    tool_name: toolName?.trim() || undefined,
    tool_description: toolDescription?.trim() || undefined,
    parameters: parameters
      .filter((p) => p.name.trim())
      .map((p) => ({ name: p.name.trim(), type: p.type, description: p.description, required: p.required })),
    current_code: code.trim() ? code : undefined,
  };
  const latest = useRef({ form, key: JSON.stringify(form) });
  latest.current = { form, key: JSON.stringify(form) };

  const generate = useCallback(
    async (brief: string) => {
      lastResult.current = null;
      const sent = latest.current;
      const declared = sent.form.parameters?.length ?? 0;
      if (declared > MAX_PARAMETERS) {
        toast({
          title: t_i18n("Too many inputs to generate the code"),
          description: t_i18n("Code can be generated for a tool with up to {max} inputs. This one declares {count}.", {
            max: MAX_PARAMETERS,
            count: declared,
          }),
          variant: "error",
        });
        throw new Error("too many inputs");
      }
      const result = await generateCode.mutateAsync({ description: brief, ...sent.form });
      if (latest.current.key !== sent.key) {
        toast({
          title: t_i18n("The editor changed while the code was being written"),
          description: t_i18n("The generated code was not applied, so your edits are kept. Generate again to start from them."),
          variant: "info",
        });
        throw new Error("stale generation");
      }
      lastResult.current = result;
      if (result.truncated) {
        toast({
          title: t_i18n("The code may be incomplete"),
          description: t_i18n("The answer reached its length limit. Review the end of the code before saving."),
          variant: "info",
        });
      }
      if (!result.syntax_ok && !showsSyntax) {
        const message = result.syntax_error ?? "";
        toast({
          title: t_i18n("The generated code does not parse"),
          description: result.syntax_error_line
            ? t_i18n("Line {line}: {message}", { line: result.syntax_error_line, message })
            : message,
          variant: "error",
        });
      }
      return result.code;
    },
    [generateCode, showsSyntax, toast, t_i18n],
  );

  const handleGenerated = useCallback(() => {
    if (lastResult.current) onGenerated(lastResult.current);
  }, [onGenerated]);

  return (
    <PromptGenerator
      contextType="custom_tool_code"
      currentText={code}
      generate={generate}
      onGenerated={handleGenerated}
      className={className}
    />
  );
}
