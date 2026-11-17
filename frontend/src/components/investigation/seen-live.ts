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
 * The runs this tab watched live - `yarn test:unit` (`seen-live.test.ts`).
 *
 * The block a reader watched being planned must stay open once its run ends:
 * the report they waited for lands in it. Component state cannot carry that,
 * because the message streamed during the turn is replaced by the saved one
 * when the turn ends, and the block is mounted again, having seen nothing.
 * So the memory is the tab's: a reload, or a conversation opened later, starts
 * with none, and its finished runs stay collapsed.
 */
const seenLive = new Set<string>();

export function markSeenLive(runId: string | null | undefined): void {
  if (runId) seenLive.add(runId);
}

export function wasSeenLive(runId: string | null | undefined): boolean {
  return !!runId && seenLive.has(runId);
}
