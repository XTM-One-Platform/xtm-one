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
 * What the chat turn on screen is doing, for the Health companion (#8261).
 * The chat page writes it, the companion reads it; it never leaves the
 * browser. A module store rather than a context: the companion is mounted by
 * the layout, the chat deep inside a route.
 */
import { useSyncExternalStore } from "react";

import type { ChatActivity } from "@/lib/health-pet";

let current: ChatActivity = "idle";
const listeners = new Set<() => void>();

export function setChatActivity(next: ChatActivity): void {
  if (next === current) return;
  current = next;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useChatActivity(): ChatActivity {
  return useSyncExternalStore(subscribe, () => current, () => "idle");
}
