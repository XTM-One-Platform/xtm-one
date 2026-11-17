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
 * Settings > AI models > Pronunciation lexicon: how every Video Studio
 * voice-over says the platform's brand names and acronyms, under each video's
 * own entries (a video entry replaces the platform's for the same written
 * form). An administrator edits it; each save is a new version, and an older
 * version can be restored as a new one. Each term can be heard as the
 * platform's speech model says it.
 */
import { useState } from "react";
import { SettingsGroupDescription, SettingsGroupTitle } from "@/components/ui/settings-form";
import { BookA, Check, History, Pencil, RotateCcw } from "lucide-react";

import { PronunciationEntriesEditor, PronunciationEntriesList } from "@/components/video-studio/PronunciationEntries";
import { usePronunciationPlayer } from "@/components/video-studio/usePronunciationPlayer";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toast";
import {
  usePronunciationLexicon,
  usePronunciationLexiconVersions,
  useRestorePronunciationLexicon,
  useSavePronunciationLexicon,
} from "@/hooks/api/useVideoStudioApi";
import { useFormatter } from "@/i18n";
import { formatDateTime } from "@/lib/utils";
import type { PronunciationEntry } from "@/types";
import { Spinner } from "@filigran/design-system";

/** The most entries the platform lexicon holds (`lexicon.MAX_ENTRIES`). */
const LEXICON_MAX = 500;

export function PronunciationLexiconSection({ isAdmin }: { isAdmin: boolean }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const { data: lexicon, isLoading } = usePronunciationLexicon();
  const save = useSavePronunciationLexicon();
  const restore = useRestorePronunciationLexicon();
  const player = usePronunciationPlayer(null);
  const [editing, setEditing] = useState(false);
  const [history, setHistory] = useState(false);
  const [draft, setDraft] = useState<PronunciationEntry[]>([]);
  const [note, setNote] = useState("");
  const versions = usePronunciationLexiconVersions(isAdmin && history);
  const entries = lexicon?.entries ?? [];

  const startEditing = () => {
    setDraft(entries);
    setNote("");
    setEditing(true);
  };
  const failed = (title: string) => (err: unknown) =>
    toast({ title, description: err instanceof Error ? err.message : undefined, variant: "error" });
  const submit = () =>
    save.mutate(
      { entries: draft, note: note.trim() || null },
      {
        onSuccess: (saved) => {
          setEditing(false);
          toast({ title: t_i18n("Pronunciation lexicon saved as version {version}", { version: saved.version ?? 0 }) });
        },
        onError: failed(t_i18n("The pronunciation lexicon was not saved")),
      },
    );
  const restoreVersion = async (version: number) => {
    const ok = await confirm({
      title: t_i18n("Restore version {version}?", { version }),
      description: t_i18n("Its entries become a new version; nothing is written over."),
      confirmLabel: t_i18n("Restore"),
    });
    if (!ok) return;
    restore.mutate(version, {
      onSuccess: (saved) => toast({ title: t_i18n("Pronunciation lexicon saved as version {version}", { version: saved.version ?? 0 }) }),
      onError: failed(t_i18n("The version was not restored")),
    });
  };

  return (
    <div data-testid="settings.pronunciation-lexicon">
      {ConfirmEl}
      <div className="mb-3 flex items-center justify-between">
        <div>
          <SettingsGroupTitle icon={BookA}>{t_i18n("Pronunciation lexicon")}</SettingsGroupTitle>
          <SettingsGroupDescription>
            {t_i18n(
              "How every Video Studio voice-over says brand names and acronyms, through the strongest means its speech model offers. A video's own pronunciations replace an entry with the same written form; each narration and each render is heard back and checked against it.",
            )}
          </SettingsGroupDescription>
        </div>
        {isAdmin && (
          <div className="ml-4 flex shrink-0 gap-2">
            {!editing ? (
              <>
                <Button variant="ghost" size="sm" className="gap-1.5" onClick={() => setHistory(!history)} aria-expanded={history}>
                  <History className="h-3 w-3" /> {t_i18n("Versions")}
                </Button>
                <Button variant="secondary" size="sm" className="gap-1.5" onClick={startEditing} disabled={isLoading}>
                  <Pencil className="h-3 w-3" /> {t_i18n("Edit")}
                </Button>
              </>
            ) : (
              <>
                <Button size="sm" className="gap-1.5" disabled={save.isPending} onClick={submit} aria-busy={save.isPending || undefined}>
                  {save.isPending && <Spinner size="sm" tone="inherit" />}{!save.isPending && <Check className="h-3 w-3" />}
                  {t_i18n("Save as a new version")}
                </Button>
                <Button variant="ghost" size="sm" onClick={() => setEditing(false)}>
                  {t_i18n("Cancel")}
                </Button>
              </>
            )}
          </div>
        )}
      </div>
      <div className="space-y-3 rounded-sm bg-elevation-default p-3.5">
        {lexicon?.version ? (
          <p className="flex flex-wrap items-center gap-2 content-caption text-default-secondary">
            <Badge variant="secondary" className="content-caption">
              {t_i18n("Version {version}", { version: lexicon.version })}
            </Badge>
            {lexicon.created_at &&
              t_i18n("Saved {when} by {who}", { when: formatDateTime(lexicon.created_at), who: lexicon.created_by ?? t_i18n("the platform") })}
            {lexicon.note && <span className="italic">{lexicon.note}</span>}
          </p>
        ) : (
          !isLoading && <p className="content-caption text-default-secondary">{t_i18n("No entry yet.")}</p>
        )}
        {editing ? (
          <>
            <PronunciationEntriesEditor value={draft} onChange={setDraft} disabled={save.isPending} max={LEXICON_MAX} player={player} testId="settings.pronunciation-lexicon.editor" />
            <Input
              aria-label={t_i18n("What changed")}
              value={note}
              maxLength={500}
              placeholder={t_i18n("What changed (kept with the version)")}
              onChange={(e) => setNote(e.target.value)}
              disabled={save.isPending}
            />
          </>
        ) : (
          <PronunciationEntriesList entries={entries} player={player} testId="settings.pronunciation-lexicon.entries" />
        )}
      </div>
      {isAdmin && history && !editing && (
        <div className="mt-3 space-y-1" data-testid="settings.pronunciation-lexicon.versions">
          {versions.isLoading && <Spinner size="sm" tone="inherit" className="text-default-secondary" />}
          {(versions.data ?? []).map((version) => (
            <div key={version.version} className="flex items-center gap-2 content-compact">
              <span className="w-10 shrink-0 font-medium tabular-nums">v{version.version}</span>
              <span className="min-w-0 flex-1 truncate text-default-secondary">
                {[
                  version.created_at ? formatDateTime(version.created_at) : null,
                  version.created_by,
                  t_i18n("{count, plural, one {# entry} other {# entries}}", { count: version.entries }),
                  version.note,
                ]
                  .filter(Boolean)
                  .join(" \u00b7 ")}
              </span>
              {version.version !== lexicon?.version && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 gap-1 content-caption"
                  disabled={restore.isPending}
                  onClick={() => void restoreVersion(version.version)}
                >
                  <RotateCcw className="h-3 w-3" /> {t_i18n("Restore")}
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
