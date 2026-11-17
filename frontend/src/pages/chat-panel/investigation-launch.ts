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
 * Whether a change of conversation keeps the Deep Investigation mode on.
 *
 * The mode goes off with each change of conversation, but for the one a launch
 * (`?investigate=`) makes: from the conversation it left (`launchLeft`) to the
 * new chat it starts (`next` is `null`). That launch turns the mode on itself.
 * Any other change, a launch that left no conversation included, turns it off,
 * so a new chat that never came keeps no later change from doing so.
 */
export function keepsInvestigationMode(change: {
  /** The conversation on screen before the change. */
  left: string | null;
  /** The conversation on screen after it, `null` for a new chat. */
  next: string | null;
  /** The conversation a launch left for its new chat, if one is under way. */
  launchLeft: string | null;
}): boolean {
  const { left, next, launchLeft } = change;
  return launchLeft !== null && left === launchLeft && next === null;
}
