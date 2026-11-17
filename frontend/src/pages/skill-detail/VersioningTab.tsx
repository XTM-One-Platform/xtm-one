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
import { GitCompare, History, Lock, RotateCcw, Trash2, Unlock } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Pagination } from "@/components/ui/pagination";
import { TotalChip } from "@/components/ui/total-chip";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { VersionDiffDialog } from "@/components/ui/version-diff-dialog";
import {
  useDeleteSkillVersion,
  useLockSkillVersion,
  useRollbackSkillVersion,
  useSkillVersionDiff,
  useSkillVersions,
  useUnlockSkillVersion,
} from "@/hooks/api/useSkillApi";
import { formatDateTime } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { AgentSkill } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";

const PAGE_SIZE = 20;

export function VersioningTab({
  skill,
  canManage,
  isAdmin,
}: {
  skill: AgentSkill;
  canManage: boolean;
  isAdmin?: boolean;
}) {
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<number[]>([]);
  const [diffOpen, setDiffOpen] = useState(false);
  const [rollbackTarget, setRollbackTarget] = useState<number | null>(null);
  const [rollbackMessage, setRollbackMessage] = useState("");
  const [ConfirmEl, confirm] = useConfirm();
  const { toast } = useToast();
  const { t_i18n } = useFormatter();

  const versionsQuery = useSkillVersions(skill.id, {
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  });
  const rollback = useRollbackSkillVersion();
  const aiOff = useAiActionDisabled();
  const deleteVersion = useDeleteSkillVersion();
  const lockVersion = useLockSkillVersion();
  const unlockVersion = useUnlockSkillVersion();

  const fromVersion = selected.length >= 1 ? selected[0] : null;
  const toVersion = selected.length >= 2 ? selected[1] : null;
  const diffQuery = useSkillVersionDiff(skill.id, fromVersion, toVersion, diffOpen);

  const versions = versionsQuery.data?.items ?? [];
  const total = versionsQuery.data?.total ?? 0;
  const activeVersionNumber =
    versions.find((v) => v.is_active)?.version_number
    ?? null;

  const toggleSelect = (vn: number) => {
    setSelected((prev) => {
      if (prev.includes(vn)) return prev.filter((x) => x !== vn);
      if (prev.length >= 2) return [prev[1], vn];
      return [...prev, vn];
    });
  };

  const openDiff = () => {
    if (fromVersion === null || toVersion === null || fromVersion === toVersion) {
      toast({ title: t_i18n("Select two distinct versions to compare"), variant: "error" });
      return;
    }
    setDiffOpen(true);
  };

  const handleRollbackConfirm = () => {
    if (rollbackTarget === null) return;
    rollback.mutate(
      { skillId: skill.id, version: rollbackTarget, commit_message: rollbackMessage || undefined },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Rolled back to v{version}", { version: rollbackTarget }), variant: "success" });
          setRollbackTarget(null);
          setRollbackMessage("");
        },
        onError: () => toast({ title: t_i18n("Rollback failed"), variant: "error" }),
      },
    );
  };

  const handleDelete = async (version: number, isLocked: boolean) => {
    if (isLocked) {
      toast({ title: t_i18n("Unlock this version before deleting it"), variant: "error" });
      return;
    }
    const ok = await confirm({
      title: "Delete version",
      description: `Permanently delete v${version}? This cannot be undone.`,
      confirmLabel: "Delete",
      variant: "danger",
    });
    if (!ok) return;
    deleteVersion.mutate(
      { skillId: skill.id, version },
      {
        onSuccess: () => toast({ title: t_i18n("v{version} deleted", { version }), variant: "success" }),
        onError: () => toast({ title: t_i18n("Delete failed"), variant: "error" }),
      },
    );
  };

  const handleLock = (version: number) => {
    lockVersion.mutate(
      { skillId: skill.id, version },
      {
        onSuccess: () => toast({ title: t_i18n("v{version} locked", { version }), variant: "success" }),
        onError: () => toast({ title: t_i18n("Lock failed"), variant: "error" }),
      },
    );
  };

  const handleUnlock = (version: number) => {
    unlockVersion.mutate(
      { skillId: skill.id, version },
      {
        onSuccess: () => toast({ title: t_i18n("v{version} unlocked", { version }), variant: "success" }),
        onError: () => toast({ title: t_i18n("Unlock failed"), variant: "error" }),
      },
    );
  };

  return (
    <div className="space-y-4">
      {ConfirmEl}

      <Dialog open={rollbackTarget !== null} onOpenChange={(o) => { if (!o) { setRollbackTarget(null); setRollbackMessage(""); } }}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t_i18n("Rollback skill")}</DialogTitle>
            <DialogDescription>
              {t_i18n("Restore skill content to v{version}. The current content will be preserved in versioning.", { version: rollbackTarget ?? "" })}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2 py-2">
            <Label htmlFor="rollback-msg" className="content-base">{t_i18n("Commit message (optional)")}</Label>
            <Input aria-label={t_i18n("Commit message (optional)")}
              id="rollback-msg"
              value={rollbackMessage}
              onChange={(e) => setRollbackMessage(e.target.value)}
              placeholder={t_i18n("e.g. Reverting accidental change")}
              className="h-8 content-base"
              maxLength={500}
            />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" size="sm" onClick={() => { setRollbackTarget(null); setRollbackMessage(""); }}>{t_i18n("Cancel")}</Button>
            <Button
              size="sm"
              className="bg-feedback-alert-primary hover:bg-feedback-alert-primary/90 text-on-feedback-fill"
              onClick={handleRollbackConfirm}
              disabled={rollback.isPending || aiOff}
            >
              <RotateCcw className="h-3.5 w-3.5 mr-1.5" />
              {t_i18n("Rollback")}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Card className="glass-card border-elevation-subtle-soft">
        <CardContent className="p-4 space-y-4">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div className="flex items-center gap-2 content-base text-default-secondary">
              <History className="h-4 w-4" />
              {t_i18n("Skill versioning")}
              {/* The total once, at the top; the pager under the versions never repeats it (#11191). */}
              {total > 0 && <TotalChip total={total} />}
            </div>
            <div className="flex items-center gap-2">
              <span className="content-compact text-default-secondary">
                {selected.length === 0
                  ? t_i18n("Select up to 2 versions to compare")
                  : selected.length === 1
                    ? t_i18n("Select one more to compare")
                    : t_i18n("v{from} vs v{to}", { from: selected[0], to: selected[1] })}
              </span>
              <Button
                variant="outline"
                size="sm"
                className="h-8 gap-1.5"
                onClick={openDiff}
                disabled={selected.length < 2 || diffQuery.isLoading}
              >
                <GitCompare className="h-3.5 w-3.5" />
                {t_i18n("Compare")}
              </Button>
            </div>
          </div>

          <div className="space-y-2">
            {versions.map((v) => {
              const isActive = activeVersionNumber === v.version_number;
              const isChecked = selected.includes(v.version_number);
              return (
                <div key={v.id} className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 flex items-center gap-3">
                  <Switch
                    checked={isChecked}
                    onCheckedChange={() => toggleSelect(v.version_number)}
                    disabled={!isChecked && selected.length >= 2}
                    aria-label={`Select v${v.version_number} for comparison`}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <p className="content-base font-medium">v{v.version_number}</p>
                      {isActive && (
                        <Badge variant="outline" className="h-5 border-feedback-success-primary/40 text-feedback-success-primary">
                          {t_i18n("Active")}
                        </Badge>
                      )}
                      {v.is_locked && (
                        <Badge variant="outline" className="h-5 border-feedback-alert-primary/40 text-feedback-warning-primary gap-1">
                          <Lock className="h-3 w-3" />
                          {t_i18n("Locked")}
                        </Badge>
                      )}
                    </div>
                    <p className="content-compact text-default-secondary">
                      {formatDateTime(v.created_at)}
                      {v.author_name ? ` · ${v.author_name}` : ""}
                    </p>
                    {v.commit_message && <p className="content-compact text-accent mt-1 break-words">{v.commit_message}</p>}
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    {canManage && !isActive && (
                      <Tip content={t_i18n("Rollback to v{version}", { version: v.version_number })}>
                        <Button aria-label={t_i18n("Rollback to v{version}", { version: v.version_number })}
                          size="sm"
                          variant="ghost"
                          className="h-8 w-8 p-0 text-feedback-warning-primary hover:text-feedback-warning-primary"
                          onClick={() => { setRollbackTarget(v.version_number); setRollbackMessage(""); }}
                          disabled={rollback.isPending || aiOff}
                        >
                          <RotateCcw className="h-3.5 w-3.5" />
                        </Button>
                      </Tip>
                    )}
                    {isAdmin && (
                      <>
                        <Tip content={v.is_locked ? t_i18n("Unlock version") : t_i18n("Lock version")}>
                          <Button
                            size="sm"
                            variant="ghost"
                            className="h-8 w-8 p-0 text-default-secondary hover:text-default-primary"
                            onClick={() => v.is_locked ? handleUnlock(v.version_number) : handleLock(v.version_number)}
                            disabled={lockVersion.isPending || unlockVersion.isPending || aiOff}
                          >
                            {v.is_locked ? <Unlock className="h-3.5 w-3.5" /> : <Lock className="h-3.5 w-3.5" />}
                          </Button>
                        </Tip>
                        {!isActive && (
                          <Tip content={t_i18n("Delete version")}>
                            <Button aria-label={t_i18n("Delete version")}
                              size="sm"
                              variant="ghost"
                              className="h-8 w-8 p-0 text-feedback-error-primary hover:text-feedback-error-primary"
                              onClick={() => void handleDelete(v.version_number, v.is_locked)}
                              disabled={deleteVersion.isPending || aiOff}
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </Button>
                          </Tip>
                        )}
                      </>
                    )}
                  </div>
                </div>
              );
            })}
            {!versionsQuery.isLoading && versions.length === 0 && (
              <p className="content-base text-default-secondary">{t_i18n("No versioning entries yet.")}</p>
            )}
          </div>

          <Pagination page={page} pageSize={PAGE_SIZE} total={total} onChange={setPage} />
        </CardContent>
      </Card>

      <VersionDiffDialog
        open={diffOpen}
        onOpenChange={setDiffOpen}
        diff={diffQuery.data ?? null}
        title={t_i18n("Skill diff")}
      />
    </div>
  );
}
