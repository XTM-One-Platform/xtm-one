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

import { HOLD_MARKER_KEY } from "./held.ts";

/**
 * The keys of an assignment run's `trigger_context` the run detail page does
 * not show as metadata tiles: the trigger type is the header badge, the
 * payload and headers open in their own dialog, and the hold marker is the
 * Held badge, and `dedup_key` is the scheduler's own idempotency key
 * (a platform event run). Internal fields are namespaced with a `_` prefix;
 * `dispatch_via_worker` is the legacy un-prefixed name kept so runs written
 * before the prefix render cleanly too.
 */
const HIDDEN_TRIGGER_KEYS: ReadonlySet<string> = new Set([
  "type",
  "body",
  "payload",
  "headers",
  "dispatch_via_worker",
  "dedup_key",
  HOLD_MARKER_KEY,
]);

export function isTriggerMetadataKey(key: string): boolean {
  return !HIDDEN_TRIGGER_KEYS.has(key) && !key.startsWith("_");
}
