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
 * Whether XTM One Relays are on for this deployment
 * (`RELAYS_ENABLED`, read from `/auth/config`). `undefined` while that config
 * has not answered: the Relays page waits instead of saying relays are off,
 * and a routed resource is never shown as refused for a moment. Once it
 * settled, a payload without the flag (a backend that predates it) or a
 * request that failed reads as on, the server default (the degrade-to-defaults
 * policy documented on `useAuthConfig`).
 *
 * Pure helper; the hook is `useRelaysEnabled` in `@/hooks/api/useRelayApi.ts`.
 */
export function relaysEnabledFrom(
  config: { relays_enabled?: boolean } | undefined,
  configPending: boolean,
): boolean | undefined {
  if (configPending) return undefined;
  return config?.relays_enabled ?? true;
}
