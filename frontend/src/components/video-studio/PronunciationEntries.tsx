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
 * Pronunciation lexicon entries: edited (a video's own
 * `voiceover.pronunciations`, the platform's lexicon in Settings) or listed,
 * each with a play button that speaks the term as the voice-over says it
 * (`POST /video-studio/pronunciations/speak`: the same lexicon, mechanism and
 * voice cache as a narration).
 */
import { type ReactNode, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Plus, Trash2, Volume2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tip } from "@/components/ui/tooltip";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { PronunciationApplyTo, PronunciationEntry, PronunciationMatch } from "@/types";

import type { PronunciationPlayer } from "./usePronunciationPlayer";
import { PRONUNCIATION_APPLY_LABELS, PRONUNCIATION_MATCH_LABELS } from "./video-format";
import { Spinner } from "@filigran/design-system";

/** The longest written form, reading and IPA (`spec.Pronunciation`). */
const WRITTEN_MAX = 80;
const SAID_MAX = 160;
const IPA_MAX = 200;
const ROW = "grid grid-cols-[1rem_minmax(0,1fr)_minmax(0,1fr)_1.75rem_1.75rem] items-center gap-1.5";

/** An entry as stored: trimmed, the defaults left out. */
function cleaned(entry: PronunciationEntry): PronunciationEntry {
  const out: PronunciationEntry = { text: entry.text.trim(), say: entry.say.trim() };
  if (entry.ipa?.trim()) out.ipa = entry.ipa.trim();
  if (entry.match && entry.match !== "word") out.match = entry.match;
  if (entry.apply_to && entry.apply_to !== "voice") out.apply_to = entry.apply_to;
  return out;
}

/** The play button of a term: how it was sent the last time it was spoken, on hover. */
function PlayTermButton({ text, player }: { text: string; player: PronunciationPlayer }) {
  const { t_i18n } = useFormatter();
  const aiOff = useAiActionDisabled();
  const spoken = player.heard[text];
  const tip = spoken
    ? t_i18n('Sent as "{sent}" ({mechanism})', { sent: spoken.sent, mechanism: spoken.mechanism ?? t_i18n("as written") })
    : t_i18n("Hear how the voice says it");
  return (
    <Tip content={tip}>
      <Button
        variant="ghost"
        size="sm"
        className="h-7 w-7 p-0"
        aria-label={t_i18n("Hear {term}", { term: text })}
        disabled={aiOff || !text.trim() || player.playing !== null}
        onClick={() => void player.play(text)}
        data-testid="pronunciation.play" aria-busy={(player.playing === text) || undefined}>
        {player.playing === text && <Spinner size="sm" tone="inherit" />}{!(player.playing === text) && <Volume2 className="h-3 w-3" />}
      </Button>
    </Tip>
  );
}

function EntryDetails({
  row,
  onChange,
  disabled,
}: {
  row: PronunciationEntry;
  onChange: (row: PronunciationEntry) => void;
  disabled: boolean;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="grid grid-cols-1 gap-1.5 pb-1 pl-[1.375rem] sm:grid-cols-3">
      <Input
        aria-label={t_i18n("IPA")}
        value={row.ipa ?? ""}
        placeholder={t_i18n("IPA (optional)")}
        onChange={(e) => onChange({ ...row, ipa: e.target.value.slice(0, IPA_MAX) })}
        disabled={disabled}
        className="font-mono-plex"
      />
      <Select value={row.match ?? "word"} onValueChange={(value) => onChange({ ...row, match: value as PronunciationMatch })} disabled={disabled}>
        <SelectTrigger aria-label={t_i18n("Match")} className="h-9">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {(Object.keys(PRONUNCIATION_MATCH_LABELS) as PronunciationMatch[]).map((key) => (
            <SelectItem key={key} value={key}>
              {t_i18n(PRONUNCIATION_MATCH_LABELS[key])}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Select
        value={row.apply_to ?? "voice"}
        onValueChange={(value) => onChange({ ...row, apply_to: value as PronunciationApplyTo })}
        disabled={disabled}
      >
        <SelectTrigger aria-label={t_i18n("Applies to")} className="h-9">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {(Object.keys(PRONUNCIATION_APPLY_LABELS) as PronunciationApplyTo[]).map((key) => (
            <SelectItem key={key} value={key}>
              {t_i18n(PRONUNCIATION_APPLY_LABELS[key])}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

/**
 * Edits a list of entries. A row being typed stays here until both its words
 * are there: a half row never reaches *onChange*.
 */
export function PronunciationEntriesEditor({
  value,
  onChange,
  disabled,
  max,
  player,
  testId,
}: {
  value: PronunciationEntry[];
  onChange: (rows: PronunciationEntry[]) => void;
  disabled: boolean;
  max: number;
  player?: PronunciationPlayer;
  testId?: string;
}) {
  const { t_i18n } = useFormatter();
  const [rows, setRows] = useState<PronunciationEntry[]>(value);
  const [open, setOpen] = useState<Set<number>>(new Set());
  const stored = JSON.stringify(value);
  useEffect(() => {
    setRows(JSON.parse(stored) as PronunciationEntry[]);
  }, [stored]);
  const update = (next: PronunciationEntry[]) => {
    setRows(next);
    onChange(next.filter((r) => r.text.trim() && r.say.trim()).map(cleaned));
  };
  const toggle = (index: number) =>
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  // The library's input draws an "N/max" counter under itself for a `maxLength`: the limits are applied while typing.
  return (
    <div className="space-y-1.5" data-testid={testId}>
      {rows.length > 0 && (
        <div className={cn(ROW, "content-caption text-default-secondary")} aria-hidden>
          <span />
          <span>{t_i18n("As written")}</span>
          <span>{t_i18n("As said")}</span>
        </div>
      )}
      {rows.map((row, index) => (
        <div key={index} className="space-y-1">
          <div className={ROW}>
            <Button
              variant="ghost"
              size="sm"
              className="h-6 w-4 p-0"
              aria-label={t_i18n("More options")}
              aria-expanded={open.has(index)}
              onClick={() => toggle(index)}
            >
              {open.has(index) ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            </Button>
            <Input
              aria-label={t_i18n("As written")}
              value={row.text}
              placeholder={t_i18n("As written")}
              onChange={(e) => update(rows.map((r, i) => (i === index ? { ...r, text: e.target.value.slice(0, WRITTEN_MAX) } : r)))}
              disabled={disabled}
            />
            <Input
              aria-label={t_i18n("As said")}
              value={row.say}
              placeholder={t_i18n("As said")}
              onChange={(e) => update(rows.map((r, i) => (i === index ? { ...r, say: e.target.value.slice(0, SAID_MAX) } : r)))}
              disabled={disabled}
            />
            {player ? <PlayTermButton text={row.text} player={player} /> : <span />}
            <Button
              variant="ghost"
              size="sm"
              className="h-7 w-7 p-0"
              aria-label={t_i18n("Remove")}
              onClick={() => update(rows.filter((_r, i) => i !== index))}
              disabled={disabled}
            >
              <Trash2 className="h-3 w-3" />
            </Button>
          </div>
          {open.has(index) && (
            <EntryDetails row={row} disabled={disabled} onChange={(next) => update(rows.map((r, i) => (i === index ? next : r)))} />
          )}
        </div>
      ))}
      <Button
        variant="outline"
        size="sm"
        className="h-7 gap-1 content-compact"
        onClick={() => setRows([...rows, { text: "", say: "" }])}
        disabled={disabled || rows.length >= max}
      >
        <Plus className="h-3 w-3" /> {t_i18n("Add a word")}
      </Button>
    </div>
  );
}

/** Entries listed, not edited: each written form, its reading and its IPA, a play button, an action per row. */
export function PronunciationEntriesList({
  entries,
  player,
  action,
  testId,
}: {
  entries: PronunciationEntry[];
  player?: PronunciationPlayer;
  action?: (entry: PronunciationEntry) => ReactNode;
  testId?: string;
}) {
  return (
    <ul className="space-y-0.5" data-testid={testId}>
      {entries.map((entry) => (
        <li key={`${entry.text}-${entry.match ?? "word"}`} className="flex items-center gap-1.5 content-compact">
          <span className="min-w-0 flex-1 truncate">
            <span className="font-medium">{entry.text}</span>
            <span className="text-default-secondary"> {"\u2192"} {entry.say}</span>
            {entry.ipa && <span className="font-mono-plex text-[11px] text-default-secondary"> /{entry.ipa}/</span>}
          </span>
          {player && <PlayTermButton text={entry.text} player={player} />}
          {action?.(entry)}
        </li>
      ))}
    </ul>
  );
}
