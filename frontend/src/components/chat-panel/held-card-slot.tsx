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

import type { ReactNode } from "react";

/**
 * The held card of a resumed cut under the transcript. Rendered by the panel
 * after its rows, so it reads `inPlace` once the rows asked `replaceMessage`
 * and `renderAboveMessage`: from the render the panel draws the card in a row
 * (the cut reply's own, or above the resumed reply), it draws nothing, never
 * two cards and never a jump for one commit (#10910, UX rounds 2 B2, 3 B3).
 */
export function HeldCardSlot({ card, inPlace }: { card: ReactNode; inPlace: () => boolean }) {
  return inPlace() ? null : card;
}
