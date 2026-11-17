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
 * Review screen for a multi-skill archive import.
 *
 * The archive is validated server-side first; this dialog shows what was
 * found and what was ignored, lets the user pick the skills and the
 * per-skill collision behaviour, and only then approves the import.
 */

import { useMemo, useState } from "react";
import {
  AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, FileWarning, XCircle,
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
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from "@/components/ui/select";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type {
  BulkSkillCollisionPolicy,
  BulkSkillIgnoreReason,
  SkillBulkImportResponse,
  SkillBulkPackageValidationResponse,
} from "@/types";

interface SkillBulkImportDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  fileName: string;
  validation: SkillBulkPackageValidationResponse | null;
  result: SkillBulkImportResponse | null;
  isImporting: boolean;
  onConfirm: (
    selectedPaths: string[],
    collisionOverrides: Record<string, BulkSkillCollisionPolicy>,
  ) => void;
}

const IGNORE_REASON_LABELS: Record<BulkSkillIgnoreReason, string> = {
  no_frontmatter: "No skill frontmatter (name + description)",
  unsupported_file_type: "Not a skill document",
  not_a_skill_root: "Not at a skill root",
  archive_metadata: "Archive metadata",
  too_large: "File too large",
  invalid_encoding: "Not valid UTF-8",
  ambiguous_owner: "Resource with no single owning skill",
};

const OUTCOME_VARIANT = {
  created: "success",
  replaced: "cyan",
  skipped: "outline",
  failed: "destructive",
} as const;

export function SkillBulkImportDialog({
  open,
  onOpenChange,
  fileName,
  validation,
  result,
  isImporting,
  onConfirm,
}: SkillBulkImportDialogProps) {
  const { t_i18n } = useFormatter();
  const [deselected, setDeselected] = useState<Set<string>>(new Set());
  const [overrides, setOverrides] = useState<Record<string, BulkSkillCollisionPolicy>>({});
  const [showIgnored, setShowIgnored] = useState(false);
  const [prevValidation, setPrevValidation] =
    useState<SkillBulkPackageValidationResponse | null>(null);

  // Reset the selection whenever a new archive is validated.
  if (validation !== prevValidation) {
    setPrevValidation(validation);
    setDeselected(new Set());
    setOverrides({});
    setShowIgnored(false);
  }

  const skills = useMemo(() => validation?.skills ?? [], [validation]);
  const importable = useMemo(() => skills.filter((skill) => skill.importable), [skills]);
  const selectedPaths = useMemo(
    () => importable.filter((skill) => !deselected.has(skill.path)).map((skill) => skill.path),
    [importable, deselected],
  );

  const ignoredByReason = useMemo(() => {
    const groups = new Map<BulkSkillIgnoreReason, string[]>();
    for (const entry of validation?.ignored ?? []) {
      const paths = groups.get(entry.reason) ?? [];
      paths.push(entry.path);
      groups.set(entry.reason, paths);
    }
    return [...groups.entries()];
  }, [validation]);

  const toggle = (path: string) => {
    setDeselected((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const setPolicy = (path: string, policy: BulkSkillCollisionPolicy) => {
    setOverrides((current) => ({ ...current, [path]: policy }));
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>
            {result ? t_i18n("Import results") : t_i18n("Review skills to import")}
          </DialogTitle>
          <DialogDescription>{fileName}</DialogDescription>
        </DialogHeader>

        {result ? (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="success">
                {t_i18n("{count} imported", { count: result.created_count })}
              </Badge>
              {result.replaced_count > 0 && (
                <Badge variant="cyan">
                  {t_i18n("{count} replaced", { count: result.replaced_count })}
                </Badge>
              )}
              {result.skipped_count > 0 && (
                <Badge variant="outline">
                  {t_i18n("{count} skipped", { count: result.skipped_count })}
                </Badge>
              )}
              {result.failed_count > 0 && (
                <Badge variant="destructive">
                  {t_i18n("{count} failed", { count: result.failed_count })}
                </Badge>
              )}
            </div>
            <div className="max-h-[50vh] space-y-1 overflow-y-auto">
              {result.results.map((outcome) => (
                <div
                  key={outcome.path}
                  className="flex items-start gap-3 rounded-sm border border-elevation-subtle px-3 py-2 content-base"
                >
                  <Badge variant={OUTCOME_VARIANT[outcome.status]}>
                    {outcome.status === "created" && t_i18n("Created")}
                    {outcome.status === "replaced" && t_i18n("Replaced")}
                    {outcome.status === "skipped" && t_i18n("Skipped")}
                    {outcome.status === "failed" && t_i18n("Failed")}
                  </Badge>
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-medium">{outcome.name ?? outcome.path}</div>
                    <div className="truncate content-compact text-default-secondary">{outcome.path}</div>
                    {outcome.message && (
                      <div className="mt-1 content-compact text-default-secondary">{outcome.message}</div>
                    )}
                  </div>
                </div>
              ))}
            </div>
            <div className="flex justify-end">
              <Button onClick={() => onOpenChange(false)}>{t_i18n("Close")}</Button>
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="cyan">
                {t_i18n("{count} skills found", { count: validation?.skill_count ?? 0 })}
              </Badge>
              {(validation?.skill_count ?? 0) !== importable.length && (
                <Badge variant="warning">
                  {t_i18n("{count} not importable", {
                    count: (validation?.skill_count ?? 0) - importable.length,
                  })}
                </Badge>
              )}
              <Badge variant="outline">
                {t_i18n("{count, plural, one {# file ignored} other {# files ignored}}", { count: validation?.ignored_count ?? 0 })}
              </Badge>
            </div>

            <div className="max-h-[45vh] space-y-1 overflow-y-auto">
              {skills.map((skill) => {
                const selected = skill.importable && !deselected.has(skill.path);
                return (
                  <div
                    key={skill.path}
                    className={cn(
                      "flex items-start gap-3 rounded-sm border px-3 py-2",
                      skill.importable ? "border-elevation-subtle" : "border-feedback-error-primary/40 bg-feedback-error-primary/5",
                    )}
                  >
                    <Switch
                      className="mt-1 shrink-0"
                      checked={selected}
                      disabled={!skill.importable || isImporting}
                      onCheckedChange={() => toggle(skill.path)}
                      aria-label={skill.name ?? skill.path}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="truncate font-medium">{skill.name ?? skill.path}</span>
                        {skill.collision && (
                          <Badge variant="warning">{t_i18n("Name already exists")}</Badge>
                        )}
                      </div>
                      <div className="truncate content-compact text-default-secondary">{skill.path}</div>
                      {skill.description && (
                        <div className="mt-1 line-clamp-2 content-compact text-default-secondary">
                          {skill.description}
                        </div>
                      )}
                      <div className="mt-1 content-compact text-default-secondary">
                        {t_i18n("{resources} resources · {versions} versions", {
                          resources: skill.resource_count,
                          versions: skill.version_count,
                        })}
                      </div>
                      {skill.errors.map((error) => (
                        <div key={error} className="mt-1 flex items-start gap-1 content-compact text-feedback-error-primary">
                          <XCircle className="mt-0.5 h-3 w-3 shrink-0" />
                          <span>{error}</span>
                        </div>
                      ))}
                      {skill.warnings.map((warning) => (
                        <div key={warning} className="mt-1 flex items-start gap-1 content-compact text-feedback-warning-primary">
                          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                          <span>{warning}</span>
                        </div>
                      ))}
                    </div>
                    {skill.importable && skill.collision && (
                      <Select
                        value={overrides[skill.path] ?? "skip"}
                        onValueChange={(value) =>
                          setPolicy(skill.path, value as BulkSkillCollisionPolicy)
                        }
                        disabled={isImporting}
                      >
                        <SelectTrigger className="w-40 shrink-0">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="skip">{t_i18n("Skip")}</SelectItem>
                          <SelectItem value="replace">{t_i18n("Replace")}</SelectItem>
                          <SelectItem value="as_new">{t_i18n("Import as new")}</SelectItem>
                        </SelectContent>
                      </Select>
                    )}
                  </div>
                );
              })}
            </div>

            {ignoredByReason.length > 0 && (
              <div className="rounded-sm border border-elevation-subtle">
                <button
                  type="button"
                  className="flex w-full items-center gap-2 px-3 py-2 content-base"
                  onClick={() => setShowIgnored((current) => !current)}
                >
                  {showIgnored ? (
                    <ChevronDown className="h-4 w-4" />
                  ) : (
                    <ChevronRight className="h-4 w-4" />
                  )}
                  <FileWarning className="h-4 w-4 text-default-secondary" />
                  {t_i18n("Ignored files")}
                  <span className="text-default-secondary">({validation?.ignored_count ?? 0})</span>
                </button>
                {showIgnored && (
                  <div className="max-h-48 space-y-3 overflow-y-auto border-t border-elevation-subtle px-3 py-2">
                    {ignoredByReason.map(([reason, paths]) => (
                      <div key={reason}>
                        <div className="content-compact font-medium">
                          {t_i18n(IGNORE_REASON_LABELS[reason] ?? reason)}{" "}
                          <span className="text-default-secondary">({paths.length})</span>
                        </div>
                        <ul className="mt-1 space-y-0.5">
                          {paths.map((path) => (
                            <li key={path} className="truncate content-compact text-default-secondary">
                              {path}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            <div className="flex items-center justify-between gap-3">
              <span className="content-compact text-default-secondary">
                {t_i18n("{count} skills selected", { count: selectedPaths.length })}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  onClick={() => onOpenChange(false)}
                  disabled={isImporting}
                >
                  {t_i18n("Cancel")}
                </Button>
                <Button
                  className="gap-1.5"
                  disabled={selectedPaths.length === 0 || isImporting}
                  onClick={() => onConfirm(selectedPaths, overrides)}
                  aria-busy={isImporting || undefined}
                >
                  {isImporting && <Spinner size="sm" tone="inherit" />}
                  {!isImporting && <CheckCircle2 className="h-4 w-4" />}
                  {t_i18n("Import {count} skills", { count: selectedPaths.length })}
                </Button>
              </div>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
