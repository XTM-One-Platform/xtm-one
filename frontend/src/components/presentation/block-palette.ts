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
 * The block palette: the blocks a person may add by hand to one
 * run, as menu items - the same list wherever it is offered (the selected
 * block's bar and its inspector, the stage's "+" at the end of a run, the
 * slide inspector's "Add a block"). What a run takes is `blockAllowedIn`; a
 * run at its cap (`canGrowRun`) offers every entry disabled rather than a 422
 * at the save.
 */
import type { LucideIcon } from "lucide-react";

import type { OverflowMenuItem } from "@/components/ui/overflow-menu";
import type { DeckBlockType } from "@/types";

import { BLOCK_ICONS } from "./block-icons";
import { BLOCK_SCHEMA, INSERTABLE_BLOCK_TYPES, blockAllowedIn } from "./block-schema";
import { canGrowRun, type DeckEditor } from "./presentation-editor";

/** The palette for the run at `listPath` on slide `slideIndex`, each entry inserting its block at `at`. */
export function blockPaletteItems(
  editor: DeckEditor,
  slideIndex: number,
  listPath: string,
  at: number,
  t: (id: string) => string,
): OverflowMenuItem[] {
  const slide = editor.draft.slides[slideIndex];
  const full = !slide || !canGrowRun(slide, listPath);
  return INSERTABLE_BLOCK_TYPES.filter((type) => blockAllowedIn(type, listPath)).map((type) => ({
    id: `insert-${type}`,
    label: t(BLOCK_SCHEMA[type].label),
    icon: BLOCK_ICONS[type] as LucideIcon,
    disabled: full,
    onSelect: () => {
      const create = BLOCK_SCHEMA[type as DeckBlockType].create;
      if (create) editor.insertBlock(slideIndex, listPath, at, create());
    },
  }));
}
