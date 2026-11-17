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

import { useState, useMemo } from "react";
import { Play, Variable } from "lucide-react";
import { Spinner } from "@filigran/design-system";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { SearchableSelect } from "@/components/ui/searchable-select";
import type { ManualTriggerParameter } from "@/types";

interface ManualTriggerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  parameters: ManualTriggerParameter[];
  assignmentName: string;
  isPending: boolean;
  onTrigger: (payload: Record<string, unknown>) => void;
}

export function ManualTriggerDialog({
  open,
  onOpenChange,
  parameters,
  assignmentName,
  isPending,
  onTrigger,
}: ManualTriggerDialogProps) {
  const defaults = useMemo(() => {
    const d: Record<string, unknown> = {};
    for (const p of parameters) {
      if (p.type === "boolean") {
        d[p.key] = p.default === "true";
      } else if (p.type === "number") {
        d[p.key] = p.default ? Number(p.default) : "";
      } else {
        d[p.key] = p.default ?? "";
      }
    }
    return d;
  }, [parameters]);

  const [values, setValues] = useState<Record<string, unknown>>(defaults);
  const [prevOpen, setPrevOpen] = useState(false);
  if (open && !prevOpen) {
    setValues(defaults);
  }
  if (open !== prevOpen) {
    setPrevOpen(open);
  }

  const setValue = (key: string, value: unknown) =>
    setValues((prev) => ({ ...prev, [key]: value }));

  const canSubmit = parameters.every((p) => {
    if (!p.required) return true;
    const v = values[p.key];
    if (v === undefined || v === null || v === "") return false;
    return true;
  });

  const handleSubmit = () => {
    const payload: Record<string, unknown> = { trigger_type: "manual" };
    for (const p of parameters) {
      const raw = values[p.key];
      if (p.type === "number" && raw !== "" && raw !== undefined) {
        payload[p.key] = Number(raw);
      } else if (p.type === "boolean") {
        payload[p.key] = !!raw;
      } else {
        payload[p.key] = raw ?? "";
      }
    }
    onTrigger(payload);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent/10">
              <Variable className="h-5 w-5 text-accent" />
            </div>
            <div>
              <DialogTitle>Run {assignmentName}</DialogTitle>
              <DialogDescription className="mt-1">
                Fill in the parameters below before triggering.
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="space-y-4 py-2">
          {parameters.map((p) => (
            <div key={p.key} className="space-y-1.5">
              <label className="content-compact font-medium flex items-center gap-1.5">
                {p.label}
                {p.required && <span className="text-feedback-error-primary">*</span>}
              </label>
              {p.description && (
                <p className="content-caption text-default-secondary leading-relaxed">
                  {p.description}
                </p>
              )}

              {p.type === "text" && (
                <Input aria-label={p.label}
                  className="h-8 content-compact"
                  placeholder={p.label}
                  value={String(values[p.key] ?? "")}
                  onChange={(e) => setValue(p.key, e.target.value)}
                />
              )}

              {p.type === "textarea" && (
                <textarea
                  className="w-full min-h-[80px] rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 content-compact text-default-primary placeholder:text-default-secondary focus:outline-hidden focus-visible:ring-2 focus-visible:ring-focus resize-y"
                  placeholder={p.label}
                  value={String(values[p.key] ?? "")}
                  onChange={(e) => setValue(p.key, e.target.value)}
                />
              )}

              {p.type === "number" && (
                <Input aria-label={p.label}
                  type="number"
                  className="h-8 content-compact w-40"
                  placeholder={p.label}
                  value={String(values[p.key] ?? "")}
                  onChange={(e) => setValue(p.key, e.target.value)}
                />
              )}

              {p.type === "boolean" && (
                <div className="flex items-center gap-2 py-1">
                  <Switch aria-label={p.label}
                    checked={!!values[p.key]}
                    onCheckedChange={(v) => setValue(p.key, v)}
                  />
                  <span className="content-compact text-default-secondary">
                    {values[p.key] ? "Yes" : "No"}
                  </span>
                </div>
              )}

              {p.type === "select" && (
                <SearchableSelect
                  aria-label={p.label}
                  searchable="auto"
                  sort={false}
                  options={(p.options ?? []).map((opt) => ({ value: opt, label: opt }))}
                  value={String(values[p.key] ?? "")}
                  onValueChange={(v) => setValue(p.key, v)}
                  placeholder={`Select ${p.label.toLowerCase()}...`}
                />
              )}
            </div>
          ))}
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onOpenChange(false)}
            disabled={isPending}
          >
            Cancel
          </Button>
          <Button
            size="sm"
            className="gap-1.5 bg-accent text-negative-primary hover:bg-accent/90"
            onClick={handleSubmit}
            disabled={!canSubmit || isPending}
            aria-busy={isPending || undefined}
          >
            {isPending && <Spinner size="sm" tone="inherit" />}
            {!isPending && <Play className="h-3.5 w-3.5" />}
            Run Now
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
