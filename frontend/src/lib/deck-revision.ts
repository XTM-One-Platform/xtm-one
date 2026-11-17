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

import { replaceEqualDeep } from "@tanstack/react-query";

type DeckStamp = { id: string; revision: number };

/**
 * Which copy of a deck the query cache keeps when a write answers.
 *
 * A deck's revision only ever grows, and a write's answer can arrive late: a
 * save committed as N+1 may be answered after the viewer's poll already
 * loaded a collaborator's N+2. The cache must never roll back to the older
 * answer - the stage would lose the collaborator's change until the next
 * poll. The same revision is still replaced: a sharing change does not bump
 * it, and its answer carries the new grants.
 */
export function newestDeck<T extends DeckStamp>(cached: T | undefined, answer: T): T {
  return cached && cached.id === answer.id && cached.revision > answer.revision ? cached : answer;
}

const isDeckStamp = (value: unknown): value is DeckStamp =>
  typeof value === "object" &&
  value !== null &&
  typeof (value as DeckStamp).id === "string" &&
  typeof (value as DeckStamp).revision === "number";

/**
 * The deck query's structural sharing: whatever writes into the cached
 * deck - a poll, a refetch, a write's answer - the NEWEST revision is kept
 * (`newestDeck`). TanStack writes a read's answer when it lands, over whatever
 * the cache holds by then, so a read sent before a save committed and
 * answered after the save's response would otherwise put the pre-save deck
 * back on the stage, and the editor with it, until the next poll. Any other
 * answer is shared the way TanStack's default does (`replaceEqualDeep`), so a
 * poll that brings nothing new still costs no re-render.
 */
export function shareNewestDeck(cached: unknown, answer: unknown): unknown {
  if (isDeckStamp(cached) && isDeckStamp(answer) && newestDeck(cached, answer) === cached) return cached;
  return replaceEqualDeep(cached, answer);
}
