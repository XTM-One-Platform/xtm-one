// SPDX-License-Identifier: Apache-2.0

import { useQuery } from "@tanstack/react-query";
import { checkSession } from "@/hooks/api/useAuthApi";
import type { User } from "@/types";

/**
 * The group that switches AI off for the signed-in person, or `null` when none
 * does. An empty string stands for a group the server did not name.
 *
 * Reads the `["me"]` query without fetching it (`enabled: false`): the profile
 * is loaded by the shell, and this hook also runs on screens that render before
 * a session exists, where it must stay silent. It still carries the query
 * function: an observer without one leaves the shared query unable to refetch
 * ("No queryFn was passed") when its options are the last applied; it is the
 * one `/auth/me` read of `useAuthApi.ts`, called lazily so a test that mocks
 * that module never reads the binding. The platform-wide switch is
 * `ai_enabled` on the platform config (`usePlatformAiEnabled`); this one decides
 * for the person, whatever the platform says. A module of its own so the
 * platform-config mocks of the component tests need not know it.
 */
export function useAiDisabledGroup(): string | null {
  const { data: me } = useQuery<User>({ queryKey: ["me"], queryFn: () => checkSession(), enabled: false });
  return me?.ai_disabled_by_group ? (me.ai_disabled_group ?? "") : null;
}
