// SPDX-License-Identifier: Apache-2.0

/**
 * Auth hook – provides the current user, role, impersonation state, and logout.
 *
 * Uses the /me endpoint to get the full user object including role.
 * The JWT deliberately carries NO role claim — /me is the single source of
 * truth for the role, and every admin action is enforced server-side. (A role
 * claim in the token would only invite client-side spoofing of admin UI.)
 *
 * When an admin impersonates a user, the /me response includes impersonation
 * metadata.  The original admin token is saved in localStorage so it can be
 * restored when the impersonation session ends.
 */

import { useCallback, useMemo } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { clearAllTokens, stopImpersonation as apiStopImpersonation } from "@/lib/api";
import { withBasePath } from "@/lib/base-path";
import { browserDraftStorage, clearAllWizardDrafts } from "@/pages/new-agent-wizard/draft-storage";
import { useMe } from "./api/useAuthApi";
import type { User } from "@/types";

interface AuthState {
  user: User | undefined;
  isLoading: boolean;
  isAdmin: boolean;
  /** Manages at least one group: reads its members' AI usage (LLM Analytics, Traces, Sessions). */
  isGroupManager: boolean;
  isAuthenticated: boolean;
  /** True when the current session is an admin impersonating another user. */
  isImpersonating: boolean;
  /** Name of the admin who is impersonating (from /me response). */
  impersonatedByName: string | null;
  logout: () => void;
  /** Stop the current impersonation session and restore the admin token. */
  stopImpersonation: () => void;
}

/** Sign out: forget the session and what it cached, then open the sign-in page. */
export function signOut(qc: QueryClient): void {
  clearAllTokens();
  clearAllWizardDrafts(browserDraftStorage());
  qc.clear();
  window.location.href = withBasePath("/login");
}

export function useAuth(): AuthState {
  const { data: user, isLoading, isSuccess } = useMe();
  const qc = useQueryClient();

  const logout = useCallback(() => signOut(qc), [qc]);

  const stopImpersonation = useCallback(() => {
    apiStopImpersonation();
    qc.clear();
    // Full reload so all queries refetch with the restored admin token
    window.location.href = withBasePath("/");
  }, [qc]);

  // `isAdmin` only becomes true once `/me` has succeeded at least once
  // (`isSuccess`), so it stays false during the initial fetch (no cached data
  // yet) and while the query is in an error state — the role comes from the
  // authoritative round-trip, not an unconfirmed/initial cache state. It does
  // intentionally remain true across background refetches of an
  // already-successful query (no admin-UI flicker). `isSuccess` and `isError`
  // are mutually exclusive, so an explicit `!isError` would be redundant.
  //
  // This is UI hardening only: the value is still read from the mutable
  // `["me"]` cache, so a devtools cache write can flip it locally — every admin
  // action is enforced server-side (`require_admin`) regardless.
  const isAdmin = useMemo(
    () => isSuccess && user?.role === "admin",
    [isSuccess, user],
  );
  const isGroupManager = useMemo(
    () => isSuccess && (user?.managed_groups?.length ?? 0) > 0,
    [isSuccess, user],
  );
  const isAuthenticated = useMemo(() => !!user, [user]);
  const isImpersonating = useMemo(() => !!user?.impersonating, [user]);
  const impersonatedByName = useMemo(() => user?.impersonated_by_name ?? null, [user]);

  return {
    user,
    isLoading,
    isAdmin,
    isGroupManager,
    isAuthenticated,
    isImpersonating,
    impersonatedByName,
    logout,
    stopImpersonation,
  };
}
