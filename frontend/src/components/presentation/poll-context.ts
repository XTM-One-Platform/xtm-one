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
 * Live poll state for the `poll` blocks of a deck (xtm-one #10157).
 *
 * `PresentationDeck` reads `/presentations/{id}/polls` every 2 s while the deck
 * holds a poll block and the reader is signed in, and provides the result
 * here; a `PollBlockView` picks its poll by id. A deck without a poll block
 * never asks. A public link or the export page has no provider: the block then
 * draws its link and QR without results. The presenter controls are NOT on the
 * slide (the audience would see them): `PollControls` draws them outside it,
 * under the editor's stage and in the speaker view.
 */
import { createContext, useContext } from "react";

import type { DeckPollBlock, DeckPollsResponse, PollState } from "@/types";

export interface DeckPollsValue {
  presentationId: string;
  data: DeckPollsResponse | undefined;
}

export const DeckPollsContext = createContext<DeckPollsValue | null>(null);

export function usePollState(pollId: string): PollState | undefined {
  const value = useContext(DeckPollsContext);
  return value?.data?.polls.find((poll) => poll.poll_id === pollId);
}

/** The poll blocks under a node (a slide, a column), in order. */
export function pollBlocksOf(node: unknown): DeckPollBlock[] {
  if (Array.isArray(node)) return node.flatMap(pollBlocksOf);
  if (node && typeof node === "object") {
    const record = node as Record<string, unknown>;
    if (record.type === "poll" && typeof record.id === "string") return [record as unknown as DeckPollBlock];
    return Object.values(record).flatMap(pollBlocksOf);
  }
  return [];
}

/** True when any slide of the spec holds a poll block (walks columns, callouts, comparisons). */
export function specHasPoll(node: unknown): boolean {
  if (Array.isArray(node)) return node.some(specHasPoll);
  if (node && typeof node === "object") {
    const record = node as Record<string, unknown>;
    if (record.type === "poll") return true;
    return Object.values(record).some(specHasPoll);
  }
  return false;
}
