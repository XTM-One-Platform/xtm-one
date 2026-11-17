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
 * What "New video" asks: the kind of video, the look, the
 * format and the brief.
 *
 * The look is free style until something asks for a template: the template a
 * template page opened the dialog with, else the one made for the kind picked.
 * A kind only suggests: the look and the format the person chose stay theirs,
 * and unpicking the kind takes its suggestion back. The templates load and
 * refetch on their own clock and never touch what was typed. Opening the
 * dialog starts afresh.
 */
import { useEffect, useMemo, useState } from "react";

import type { VideoTemplateSummary } from "@/types";

import { isTemplatePurpose, templatesForKind, VIDEO_KIND_FORMATS, type VideoKind } from "./video-format";

/** The template pick that wears none. */
export const FREESTYLE = "__freestyle__";
const ALL_RATIOS = ["16:9", "9:16", "1:1", "4:5"];

export function useNewVideoChoices(open: boolean, rows: VideoTemplateSummary[], templateId?: string | null) {
  // `undefined` until the person picks or unpicks a kind: the preset's purpose shows meanwhile.
  const [kindPick, setKindPick] = useState<VideoKind | null | undefined>(undefined);
  const [lookPick, setLookPick] = useState<string | null>(null);
  const [ratioPick, setRatioPick] = useState<string | null>(null);
  const [brief, setBrief] = useState("");

  useEffect(() => {
    if (!open) return;
    setKindPick(undefined);
    setLookPick(null);
    setRatioPick(null);
    setBrief("");
  }, [open]);

  const preset = templateId ? rows.find((row) => row.id === templateId) : undefined;
  const kind = kindPick === undefined ? (preset?.purposes[0] ?? null) : kindPick;
  const suggested = useMemo(
    () => (kind && isTemplatePurpose(kind) ? templatesForKind(rows, kind).find((row) => row.purposes.includes(kind)) : undefined),
    [rows, kind],
  );
  const chosen = lookPick === FREESTYLE || rows.some((row) => row.id === lookPick) ? lookPick : null;
  const pick = chosen ?? preset?.id ?? suggested?.id ?? FREESTYLE;
  const picked = rows.find((row) => row.id === pick) ?? null;
  const ratios = useMemo(() => (picked ? picked.aspect_ratios : ALL_RATIOS), [picked]);
  const ratio = [ratioPick, kind ? VIDEO_KIND_FORMATS[kind] : undefined, ratios[0]].find((r) => !!r && ratios.includes(r)) ?? "16:9";

  const chooseKind = (next: VideoKind) => setKindPick(kind === next ? null : next);

  return {
    kind,
    chooseKind,
    pick,
    choosePick: setLookPick,
    picked,
    suggested: suggested ?? null,
    ratio,
    setRatio: setRatioPick,
    ratios,
    brief,
    setBrief,
  };
}
