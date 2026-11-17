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
 * Execution dialogs.
 *
 * `RunParamsDialog` — shown before a manual run when the flow's
 * `trigger.manual` node declares input parameters; the values become the
 * trigger step's outputs (`{{<trigger>.<key>}}`).
 *
 * `ActivationInfoDialog` — shown after activating a flow that carries a
 * webhook trigger. The shared secret appears in the activation response ONLY
 * (never in list/detail payloads), so this dialog is the user's one chance to
 * copy it — which is why it is a dialog and not a toast.
 */

import { useRef, useState } from "react";
import { Paperclip, X as XIcon } from "lucide-react";
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
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { SpeechMicButton } from "./SpeechMicButton";
import { Switch } from "@/components/ui/switch";
import { useFormatter } from "@/i18n";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { cn } from "@/lib/utils";
import type { FlowActivateResult } from "@/types";
import { paramDefaultText, type DeclaredParam } from "./params";

interface RunParamsDialogProps {
  /** Default prompt from the trigger node's config — editable before running. */
  defaultPrompt: string;
  /** The manual trigger's declared parameters — the run's typed signature. */
  parameters: DeclaredParam[];
  /** True while the shell uploads the attachments and starts the run. */
  isStarting?: boolean;
  onRun: (input: {
    prompt: string;
    parameters: Record<string, string>;
    files: File[];
  }) => void;
  onClose: () => void;
}

export function RunParamsDialog({
  defaultPrompt,
  parameters,
  isStarting,
  onRun,
  onClose,
}: RunParamsDialogProps) {
  const { t_i18n } = useFormatter();
  const [prompt, setPrompt] = useState(defaultPrompt);
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      parameters
        .map((p) => [p.name, paramDefaultText(p)])
        .filter(([, v]) => v !== ""),
    ),
  );
  const [files, setFiles] = useState<File[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // The typed form's compile gate: every required parameter needs a value.
  const missingRequired = parameters.filter(
    (p) => p.required && !(values[p.name] ?? "").trim(),
  );

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t_i18n("Run with inputs")}</DialogTitle>
          {/* No braces in the copy: `{{…}}` is ICU syntax to react-intl and
              would be parsed as an argument, not shown as text. */}
          <DialogDescription>
            {t_i18n(
              "Tell this run what to work on. The flow reads it as the trigger's prompt.",
            )}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="run-prompt">
              {t_i18n("Prompt")}
            </Label>
            <div className="relative">
              <textarea
                id="run-prompt"
                rows={3}
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder={t_i18n("What should this run work on?")}
                className={cn("w-full rounded-sm border border-elevation-subtle bg-transparent px-3 py-2 pr-8 content-compact placeholder:text-default-secondary", FIELD_FOCUS_RING)}
                // The prompt is what the runner came here to type.
                autoFocus
              />
              <SpeechMicButton
                className="absolute right-1.5 top-1.5"
                onTranscript={(text) =>
                  setPrompt((prev) =>
                    prev.trim() ? `${prev.replace(/\s+$/, "")} ${text}` : text,
                  )
                }
              />
            </div>
          </div>
          {parameters.map((param) => (
            <div key={param.name} className="space-y-1">
              <Label
                htmlFor={`run-param-${param.name}`}
                className="flex items-center gap-1 font-mono-plex"
              >
                {param.name}
                <span className="font-sans-plex content-caption text-default-secondary">
                  ({param.type})
                </span>
                {param.required && <span className="text-feedback-error-primary">*</span>}
              </Label>
              {param.type === "boolean" ? (
                <Switch
                  id={`run-param-${param.name}`}
                  checked={(values[param.name] ?? "") === "true"}
                  onCheckedChange={(checked) =>
                    setValues((prev) => ({
                      ...prev,
                      [param.name]: checked ? "true" : "false",
                    }))
                  }
                />
              ) : param.type === "json" ? (
                <textarea
                  id={`run-param-${param.name}`}
                  rows={3}
                  value={values[param.name] ?? ""}
                  placeholder='{"key": "value"}'
                  onChange={(e) =>
                    setValues((prev) => ({
                      ...prev,
                      [param.name]: e.target.value,
                    }))
                  }
                  className={cn("w-full rounded-sm border border-elevation-subtle bg-transparent px-3 py-2 font-mono-plex content-compact placeholder:text-default-secondary", FIELD_FOCUS_RING)}
                />
              ) : (
                <Input aria-label={param.name}
                  id={`run-param-${param.name}`}
                  type={param.type === "number" ? "number" : "text"}
                  value={values[param.name] ?? ""}
                  onChange={(e) =>
                    setValues((prev) => ({
                      ...prev,
                      [param.name]: e.target.value,
                    }))
                  }
                />
              )}
              {param.description && (
                <p className="content-caption text-default-secondary">
                  {param.description}
                </p>
              )}
            </div>
          ))}

          {/* Run attachments — every agent node of the run receives them,
              the way chat attachments reach an agent. */}
          <div className="space-y-1">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => {
                const picked = Array.from(e.target.files ?? []);
                e.target.value = "";
                if (picked.length) setFiles((prev) => [...prev, ...picked]);
              }}
            />
            <Button
              variant="outline"
              size="sm"
              className="gap-1.5"
              onClick={() => fileInputRef.current?.click()}
            >
              <Paperclip className="h-3.5 w-3.5" />
              {t_i18n("Attach files")}
            </Button>
            {files.length > 0 && (
              <div className="flex flex-wrap gap-1 pt-1">
                {files.map((f, i) => (
                  <span
                    key={`${f.name}-${i}`}
                    className="flex items-center gap-1 rounded-sm border border-elevation-subtle px-1.5 py-0.5 content-caption text-default-secondary"
                  >
                    <span className="max-w-40 truncate">{f.name}</span>
                    <button
                      type="button"
                      aria-label={t_i18n("Remove")}
                      onClick={() =>
                        setFiles((prev) => prev.filter((_, j) => j !== i))
                      }
                    >
                      <XIcon className="h-3 w-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={isStarting}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            disabled={isStarting || missingRequired.length > 0}
            title={
              missingRequired.length > 0
                ? t_i18n("Fill the required parameters first.")
                : undefined
            }
            onClick={() => onRun({ prompt, parameters: values, files })}
          >
            {isStarting ? t_i18n("Starting…") : t_i18n("Run")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

interface ActivationInfoDialogProps {
  result: FlowActivateResult;
  onClose: () => void;
}

export function ActivationInfoDialog({
  result,
  onClose,
}: ActivationInfoDialogProps) {
  const { t_i18n } = useFormatter();

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t_i18n("Flow activated")}</DialogTitle>
          <DialogDescription>
            {t_i18n(
              "Send deliveries to the URL below, signed with the secret. The secret is shown on activation only: store it now.",
            )}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          {result.webhook_url && (
            <div className="space-y-1">
              <Label>{t_i18n("Webhook URL")}</Label>
              <div className="flex items-center gap-1">
                <code className="min-w-0 flex-1 truncate rounded-sm bg-elevation-hover px-2 py-1.5 content-compact">
                  {result.webhook_url}
                </code>
                <CopyIconButton value={result.webhook_url} label={t_i18n("Copy the webhook URL")} />
              </div>
            </div>
          )}
          {result.webhook_secret && (
            <div className="space-y-1">
              <Label>{t_i18n("Webhook secret")}</Label>
              <div className="flex items-center gap-1">
                <code className="min-w-0 flex-1 truncate rounded-sm bg-elevation-hover px-2 py-1.5 content-compact">
                  {result.webhook_secret}
                </code>
                <CopyIconButton value={result.webhook_secret} label={t_i18n("Copy the webhook secret")} />
              </div>
            </div>
          )}
          {result.warnings.map((warning) => (
            <p key={warning.code} className="content-compact text-feedback-warning-primary">
              {warning.message}
            </p>
          ))}
        </div>
        <div className="flex justify-end">
          <Button onClick={onClose}>{t_i18n("Done")}</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
