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

/** Pure helpers shared by the block and layout renderers. */
import type { DeckAlign } from "@/types";

/** Download URL of a platform file (template asset or generated artifact). */
export function fileUrl(fileId: string | undefined | null): string | null {
  return fileId ? `/api/v1/chat/files/${fileId}/download` : null;
}

/** `data-align` value: `start` is the CSS default and is left unset. */
export function alignAttr(align: DeckAlign | undefined): DeckAlign | undefined {
  return align && align !== "start" ? align : undefined;
}
