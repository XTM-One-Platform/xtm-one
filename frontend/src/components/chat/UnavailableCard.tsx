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
 * The one "no longer available" card of the chat: what a chat card
 * renders when the object its marker names cannot be read any more (deleted,
 * unshared, never shared). Every chat card (video, presentation, report,
 * investigation, scheduled work item) renders THIS card for that state, so a
 * deleted object reads the same in a conversation whatever its kind: same
 * frame, icon, padding and type scale.
 *
 * The card takes the sentence already translated, so each caller keeps its
 * own translation key, one sentence per kind of object.
 * `unavailable-card.test.ts` fails when that copy is rendered anywhere else
 * under `components/`.
 */
import { Icon } from "@filigran/design-system";

export function UnavailableCard({ message }: { message: string }) {
  return (
    <div
      role="status"
      data-testid="chat-card.unavailable"
      className="my-2 flex items-center gap-2 rounded-sm border border-elevation-subtle layer-1 bg-elevation-default px-3 py-2.5"
    >
      <Icon name="ban" size={16} className="text-default-secondary" />
      <span className="content-compact text-default-secondary">{message}</span>
    </div>
  );
}
